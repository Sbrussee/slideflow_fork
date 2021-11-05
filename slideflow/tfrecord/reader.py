"""Reader utils."""

import functools
import gzip
import io
import os
import struct
import typing
import numpy as np

from slideflow.util import log
from slideflow.tfrecord import example_pb2
from slideflow.tfrecord import iterator_utils

def tfrecord_iterator(
    data_path: str,
    index = None,
    shard: typing.Optional[typing.Tuple[int, int]] = None,
    clip: typing.Optional[int] = None,
    compression_type: typing.Optional[str] = None,
    random_start: bool = False,
    datum_bytes = None,
) -> typing.Iterable[memoryview]:
    """Create an iterator over the tfrecord dataset.

    Since the tfrecords file stores each example as bytes, we can
    define an iterator over `datum_bytes_view`, which is a memoryview
    object referencing the bytes.

    Params:
    -------
    data_path: str
        TFRecord file path.

    index: optional, default=None
        np.loadtxt(index_path, dtype=np.int64)

    shard: tuple of ints, optional, default=None
        A tuple (index, count) representing worker_id and num_workers
        count. Necessary to evenly split/shard the dataset among many
        workers (i.e. >1).

    random_start: randomize starting location of reading.
        Requires an index file. Only works if shard is None.

    Yields:
    -------
    datum_bytes_view: memoryview
        Object referencing the specified `datum_bytes` contained in the
        file (for a single record).
    """

    if compression_type == "gzip":
        file = gzip.open(data_path, "rb")
    elif compression_type is None:
        file = io.open(data_path, "rb")
    else:
        raise ValueError("compression_type should be either 'gzip' or None")
    length_bytes = bytearray(8)
    crc_bytes = bytearray(4)
    if datum_bytes is None:
        datum_bytes = bytearray(1024 * 1024)

    def read_records(start_offset=None, end_offset=None):
        nonlocal length_bytes, crc_bytes, datum_bytes

        if start_offset is not None:
            file.seek(start_offset)
        if end_offset is None:
            end_offset = os.path.getsize(data_path)
        while file.tell() < end_offset:
            if file.readinto(length_bytes) != 8:
                raise RuntimeError("Failed to read the record size.")
            if file.readinto(crc_bytes) != 4:
                raise RuntimeError("Failed to read the start token.")
            length, = struct.unpack("<Q", length_bytes)
            if length > len(datum_bytes):
                try:
                    datum_bytes = datum_bytes.zfill(int(length * 1.5))
                except OverflowError as e:
                    raise OverflowError('Error reading tfrecords; please try regenerating index files')
            datum_bytes_view = memoryview(datum_bytes)[:length]
            if file.readinto(datum_bytes_view) != length:
                raise RuntimeError("Failed to read the record.")
            if file.readinto(crc_bytes) != 4:
                raise RuntimeError("Failed to read the end token.")
            yield datum_bytes_view

    if index is None: #index_path
        yield from read_records()
    else:
        if len(index.shape) == 1: # For the case that there is only a single record in the file
            index = np.expand_dims(index, axis=0)
        index = index[:, 0]
        if clip:
            clip_offset = None if clip == len(index) else index[clip]
            index = index[:clip]
        else:
            clip_offset = None
        if shard is None and random_start:
            offset = np.random.choice(index)
            yield from read_records(offset, clip_offset)
            yield from read_records(0, offset)
        elif shard is None:
            yield from read_records(0, clip_offset)
        else:
            shard_idx, shard_count = shard
            all_shard_indices = np.array_split(index, shard_count)
            start_byte = all_shard_indices[shard_idx][0]
            if shard_idx < (shard_count-1):
                end_byte = all_shard_indices[shard_idx + 1][0]
            else:
                end_byte = clip_offset
            yield from read_records(start_byte, end_byte)

    file.close()


def process_feature(feature: example_pb2.Feature,
                    typename: str,
                    typename_mapping: dict,
                    key: str):
    # NOTE: We assume that each key in the example has only one field
    # (either "bytes_list", "float_list", or "int64_list")!
    field = feature.ListFields()[0]
    inferred_typename, value = field[0].name, field[1].value

    if typename is not None:
        tf_typename = typename_mapping[typename]
        if tf_typename != inferred_typename:
            reversed_mapping = {v: k for k, v in typename_mapping.items()}
            raise TypeError(f"Incompatible type '{typename}' for `{key}` "
                        f"(should be '{reversed_mapping[inferred_typename]}').")

    if inferred_typename == "bytes_list":
        value = np.frombuffer(value[0], dtype=np.uint8)
    elif inferred_typename == "float_list":
        value = np.array(value, dtype=np.float32)
    elif inferred_typename == "int64_list":
        value = np.array(value, dtype=np.int64)
    return value

def extract_feature_dict(features, description, typename_mapping):
    if isinstance(features, example_pb2.FeatureLists):
        features = features.feature_list

        def get_value(typename, typename_mapping, key):
            feature = features[key].feature
            fn = functools.partial(process_feature, typename=typename,
                                   typename_mapping=typename_mapping, key=key)
            return list(map(fn, feature))
    elif isinstance(features, example_pb2.Features):
        features = features.feature

        def get_value(typename, typename_mapping, key):
            return process_feature(features[key], typename,
                                   typename_mapping, key)
    else:
        raise TypeError(f"Incompatible type: features should be either of type "
                        f"example_pb2.Features or example_pb2.FeatureLists and "
                        f"not {type(features)}")

    all_keys = list(features.keys())

    if description is None or len(description) == 0:
        description = dict.fromkeys(all_keys, None)
    elif isinstance(description, list):
        description = dict.fromkeys(description, None)

    processed_features = {}
    for key, typename in description.items():
        if key not in all_keys:
            raise KeyError(f"Key {key} doesn't exist (select from {all_keys})!")

        processed_features[key] = get_value(typename, typename_mapping, key)

    return processed_features

def example_loader(
    data_path: str,
    index: None,#typing.Union[str, None],
    description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
    shard: typing.Optional[typing.Tuple[int, int]] = None,
    clip: typing.Optional[int] = None,
    compression_type: typing.Optional[str] = None,
    datum_bytes = None,
) -> typing.Iterable[typing.Dict[str, np.ndarray]]:
    """Create an iterator over the (decoded) examples contained within
    the dataset.

    Decodes raw bytes of the features (contained within the dataset)
    into its respective format.

    Params:
    -------
    data_path: str
        TFRecord file path.

    index_path: str or None
        Index file path. Can be set to None if no file is available.

    description: list or dict of str, optional, default=None
        List of keys or dict of (key, value) pairs to extract from each
        record. The keys represent the name of the features and the
        values ("byte", "float", or "int") correspond to the data type.
        If dtypes are provided, then they are verified against the
        inferred type for compatibility purposes. If None (default),
        then all features contained in the file are extracted.

    shard: tuple of ints, optional, default=None
        A tuple (index, count) representing worker_id and num_workers
        count. Necessary to evenly split/shard the dataset among many
        workers (i.e. >1).

    compression_type: str, optional, default=None
        The type of compression used for the tfrecord. Choose either
        'gzip' or None.

    Yields:
    -------
    features: dict of {str, np.ndarray}
        Decoded bytes of the features into its respective data type (for
        an individual record).
    """

    typename_mapping = {
        "byte": "bytes_list",
        "float": "float_list",
        "int": "int64_list"
    }

    record_iterator = tfrecord_iterator(
        data_path=data_path,
        index=index,
        shard=shard,
        clip=clip,
        compression_type=compression_type,
        datum_bytes=datum_bytes,
    )

    for record in record_iterator:
        example = example_pb2.Example()
        example.ParseFromString(record)
        yield extract_feature_dict(example.features, description, typename_mapping)

def sequence_loader(
    data_path: str,
    index_path: typing.Union[str, None],
    context_description: typing.Union[
        typing.List[str], typing.Dict[str, str], None
    ] = None,
    features_description: typing.Union[
        typing.List[str], typing.Dict[str, str], None
    ] = None,
    shard: typing.Optional[typing.Tuple[int, int]] = None,
    clip: typing.Optional[int] = None,
    compression_type: typing.Optional[str] = None,
) -> typing.Iterable[
    typing.Tuple[
        typing.Dict[str, np.ndarray], typing.Dict[str, typing.List[np.ndarray]]
    ]
]:
    """Create an iterator over the (decoded) sequence examples contained within
    the dataset.

    Decodes raw bytes of both the context and features (contained within the
    dataset) into its respective format.

    Params:
    -------
    data_path: str
        TFRecord file path.

    index_path: str or None
        Index file path. Can be set to None if no file is available.

    context_description: list or dict of str, optional, default=None
        List of keys or dict (key, value) pairs to extract from the
        the context of each record. The keys represent the name of the
        features and the values ("byte", "float" or "int") correspond
        to the data type. If dtypes are provided, then they are verified
        against the inferred type for compatibility purposes. If None
        (default), then all features contained in the file are extracted.

    features_description: list or dict of str, optional, default=None
        Same as `context_description`, but applies to the features of
        each record.

    shard: tuple of ints, optional, default=None
        A tuple (index, count) representing worker_id and num_workers
        count. Necessary to evenly split/shard the dataset among many
        workers (i.e. >1).

    compression_type: str, optional, default=None
        The type of compression used for the tfrecord. Choose either
        'gzip' or None.

    Yields:
    -------
    A tuple of (context, features) for an individual record.

    context: dict of {str, np.ndarray}
        Decoded bytes of the context features into its respective data
        type.

    features: dict of {str, np.ndarray}
        Decoded bytes of the sequence features into its respective data
        type.
    """
    typename_mapping = {
        "byte": "bytes_list",
        "float": "float_list",
        "int": "int64_list"
    }

    record_iterator = tfrecord_iterator(
        data_path=data_path,
        index_path=index_path,
        shard=shard,
        clip=clip,
        compression_type=compression_type,
    )

    for record in record_iterator:
        example = example_pb2.SequenceExample()
        example.ParseFromString(record)

        context = extract_feature_dict(example.context, context_description, typename_mapping)
        features = extract_feature_dict(example.feature_lists, features_description, typename_mapping)

        yield context, features


def tfrecord_loader(
    data_path: str,
    index: None,#typing.Union[str, None],
    description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
    shard: typing.Optional[typing.Tuple[int, int]] = None,
    clip: typing.Optional[int] = None,
    sequence_description: typing.Union[
        typing.List[str], typing.Dict[str, str], None
    ] = None,
    compression_type: typing.Optional[str] = None,
    datum_bytes = None,
) -> typing.Iterable[
    typing.Union[
        typing.Dict[str, np.ndarray],
        typing.Tuple[
            typing.Dict[str, np.ndarray], typing.Dict[str, typing.List[np.ndarray]]
        ],
    ]
]:
    """Create an iterator over the (decoded) examples contained within
    the dataset.

    Decodes raw bytes of the features (contained within the dataset)
    into its respective format.

    Params:
    -------
    data_path: str
        TFRecord file path.

    index_path: str or None
        Index file path. Can be set to None if no file is available.

    description: list or dict of str, optional, default=None
        List of keys or dict of (key, value) pairs to extract from each
        record. The keys represent the name of the features and the
        values ("byte", "float", or "int") correspond to the data type.
        If dtypes are provided, then they are verified against the
        inferred type for compatibility purposes. If None (default),
        or an empty list or dictionary, then all features contained in
        the file are extracted.

    shard: tuple of ints, optional, default=None
        A tuple (index, count) representing worker_id and num_workers
        count. Necessary to evenly split/shard the dataset among many
        workers (i.e. >1).

    sequence_description: list or dict of str, optional, default=None
        Similar to `description`, but refers to the sequence features
        within a `SequenceExample`. When this field is `None`, then it
        is assumed that an `Example` is being read otherwise, a
        `SequenceExample` is read. If an empty list or dictionary is
        passed, then all features contained in the file are extracted.

    compression_type: str, optional, default=None
        The type of compression used for the tfrecord. Choose either
        'gzip' or None.

    Yields:
    -------
    features: dict of {str, value}
        Decoded bytes of the features into its respective data type (for
        an individual record). `value` is either going to be an np.ndarray
        in the instance of an `Example` and a list of np.ndarray in the
        instance of a `SequenceExample`.
    """
    if sequence_description is not None:
        return sequence_loader(
            data_path=data_path,
            index_path=index,
            context_description=description,
            features_description=sequence_description,
            shard=shard,
            clip=clip,
            compression_type=compression_type
        )
    return example_loader(
        data_path=data_path,
        index=index,
        description=description,
        shard=shard,
        clip=clip,
        compression_type=compression_type,
        datum_bytes=datum_bytes
    )


def multi_tfrecord_loader(paths: typing.List[str],
                          indices,
                          splits: typing.Dict[str, float],
                          description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
                          sequence_description: typing.Union[typing.List[str], typing.Dict[str, str], None] = None,
                          compression_type: typing.Optional[str] = None,
                          shard: typing.Optional[typing.Tuple[int, int]] = None,
                          clip: typing.Optional[typing.Dict[str, int]] = None,
                          infinite: bool = True,
                          ) -> typing.Iterable[typing.Union[typing.Dict[str, np.ndarray],
                                                            typing.Tuple[typing.Dict[str, np.ndarray],
                                                                         typing.Dict[str, typing.List[np.ndarray]]]]]:
    """Create an iterator by reading and merging multiple tfrecord datasets.

    Params:
    -------
    paths: list of str
        List of tfrecord paths.

    indices: dict mapping tfrecord names to index paths.
        Input index path pattern.

    splits: dict
        Dictionary of (key, value) pairs, where the key is used to
        construct the data and index path(s) and the value determines
        the contribution of each split to the batch.

    description: list or dict of str, optional, default=None
        List of keys or dict of (key, value) pairs to extract from each
        record. The keys represent the name of the features and the
        values ("byte", "float", or "int") correspond to the data type.
        If dtypes are provided, then they are verified against the
        inferred type for compatibility purposes. If None (default),
        then all features contained in the file are extracted.

    sequence_description: list or dict of str, optional, default=None
        Similar to `description`, but refers to the sequence features
        within a `SequenceExample`. When this field is `None`, then it
        is assumed that an `Example` is being read otherwise, a
        `SequenceExample` is read. If an empty list or dictionary is
        passed, then all features contained in the file are extracted.

    compression_type: str, optional, default=None
        The type of compression used for the tfrecord. Choose either
        'gzip' or None.

    infinite: bool, optional, default=True
        Whether the returned iterator should be infinite or not

    Returns:
    --------
    it: iterator
        A repeating iterator that generates batches of data.
    """

    if indices is None and (shard is not None or clip is not None):
        log.debug(f"Index files not found for tfrecord; unable to perform clipping or sharding (data will be duplicated).")

    datum_bytes = bytearray(1024 * 1024)
    loaders = [functools.partial(tfrecord_loader, data_path=tfr_path.decode('utf-8'),
                                 index=indices[i] \
                                     if indices is not None else None,
                                 description=description,
                                 shard=shard,
                                 clip=(None if not clip else clip[i]),
                                 sequence_description=sequence_description,
                                 compression_type=compression_type,
                                 datum_bytes=datum_bytes,
                                 )
               for i, tfr_path in enumerate(paths)]
    splits_list = splits if splits is not None else np.array([0.5 for t in range(len(paths))])
    return iterator_utils.sample_iterators(loaders, splits_list, infinite=infinite, shard=shard)