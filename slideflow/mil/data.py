"""Dataset utility functions for MIL."""

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Callable, Union, Protocol
from pathlib import Path

import numpy as np
import numpy.typing as npt
import torch
from torch.utils.data import Dataset
import logging

# -----------------------------------------------------------------------------

def build_dataset(
    bags,
    targets,
    encoder,
    bag_size: int,
    use_lens: bool = False,
    survival_discrete: bool = False
    ):
    """
    Build a dataset for classification or discrete survival tasks.

    Args:
        bags: A list of bag inputs (each can be a path to a .pt file, np array, etc.).
        targets: A NumPy array or Tensor of shape [N] or [N, 2].
                 - For classification, shape is [N].
                 - For discrete survival, shape is [N, 2], where [:,0] is the discrete-time
                   label (1,2,3,4,...), and [:,1] is an event indicator (which we *ignore* here).
        encoder: An optional sklearn-like encoder (e.g. LabelEncoder, OneHotEncoder).
        bag_size: Max instances in each bag (for sampling/padding).
        use_lens: Whether to include bag lengths in the final tuple.
        survival_discrete: If True, we only keep the first column of `targets` (durations).
    """
    # If survival discrete, keep only the discrete-time index from column 0
    # ignoring the event column (targets[:,1]).
    if survival_discrete:
        targets = targets[:, 0]
        # Convert all values inside targets to int
        targets = targets.astype(int)

    assert len(bags) == len(targets)

    def _zip(bag, targets):
        features, lengths = bag
        if use_lens:
            return (features, lengths, targets.squeeze())
        else:
            return (features, targets.squeeze())

    dataset = MapDataset(
        _zip,
        BagDataset(bags, bag_size=bag_size),
        EncodedDataset(encoder, targets),
    )
    dataset.encoder = encoder
    return dataset

def build_clam_dataset(bags, targets, encoder, bag_size):
    assert len(bags) == len(targets)

    def _zip(bag, targets):
        features, lengths = bag
        return (features, targets.squeeze(), True), targets.squeeze()

    dataset = MapDataset(
        _zip,
        BagDataset(bags, bag_size=bag_size),
        EncodedDataset(encoder, targets),
    )
    dataset.encoder = encoder
    return dataset

def build_multibag_dataset(bags, targets, encoder, bag_size, n_bags, use_lens=False):
    assert len(bags) == len(targets)

    def _zip(bags_and_lengths, targets):
        if use_lens:
            return *bags_and_lengths, targets.squeeze()
        else:
            return [b[0] for b in bags_and_lengths], targets.squeeze()

    dataset = MapDataset(
        _zip,
        MultiBagDataset(bags, n_bags, bag_size=bag_size),
        EncodedDataset(encoder, targets),
    )
    dataset.encoder = encoder
    return dataset

# -----------------------------------------------------------------------------

def _to_fixed_size_bag(
    bag: torch.Tensor,
    bag_size: int = 512
) -> Tuple[torch.Tensor, int]:
    # If the bag has more than two dimensions, reduce it to [N, 2048]
    if bag.dim() > 2:
        bag_flattened = bag.mean(dim=[2, 3])  # Average over the last two dimensions
    else:
        bag_flattened = bag  # Use the bag as is if it is already 2D
    # get up to bag_size elements
    bag_idxs = torch.randperm(bag.shape[0])[:bag_size]
    bag_samples = bag[bag_idxs]

    # zero-pad if we don't have enough samples
    zero_padded = torch.cat(
        (
            bag_samples,
            torch.zeros(bag_size - bag_samples.shape[0], bag_samples.shape[1]),
        )
    )
    return zero_padded, min(bag_size, len(bag))

# -----------------------------------------------------------------------------

@dataclass
class BagDataset(Dataset):

    def __init__(
        self,
        bags: Union[List[Path], List[np.ndarray], List[torch.Tensor], List[List[str]]],
        bag_size: Optional[int] = None,
        preload: bool = False
    ):
        """A dataset of bags of instances.

        Args:

            bags (list(str), list(np.ndarray), list(torch.Tensor), list(list(str))):  Bags for each slide.
                This can either be a list of `.pt`  files, a list of numpy
                arrays, a list of Tensors, or a list of lists of strings (where
                each item in the list is a patient, and nested items are slides
                for that patient). Each bag consists of features taken from all
                images from a slide. Data should be of shape N x F, where N is
                the number of instances and F is the number of features per
                instance/slide.
            bag_size (int):  The number of instances in each bag. For bags
                containing more instances, a random sample of `bag_size`
                instances will be drawn.  Smaller bags are padded with zeros.
                If `bag_size` is None, all the samples will be used.

        """
        super().__init__()
        self.bags = bags
        self.bag_size = bag_size
        self.preload = preload

        if self.preload:
            self.bags = [self._load(i) for i in range(len(self.bags))]

    def __len__(self):
        return len(self.bags)

    def _load(self, index: int):
        if isinstance(self.bags[index], str):
            feats = torch.load(self.bags[index]).to(torch.float32)
        elif isinstance(self.bags[index], np.ndarray):
            feats = torch.from_numpy(self.bags[index]).to(torch.float32)
        elif isinstance(self.bags[index], torch.Tensor):
            feats = self.bags[index]
        else:
            feats = torch.cat([
                torch.load(slide).to(torch.float32)
                for slide in self.bags[index]
            ])
        return feats

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, int]:
        # collect all the features
        if self.preload:
            feats = self.bags[index]
        else:
            feats = self._load(index)

        # sample a subset, if required
        if self.bag_size:
            return _to_fixed_size_bag(feats, bag_size=self.bag_size)
        else:
            return feats, len(feats)

# -----------------------------------------------------------------------------

@dataclass
class MultiBagDataset(Dataset):
    """A dataset of bags of instances, with multiple bags per instance."""

    bags: List[Union[List[Path], List[np.ndarray], List[torch.Tensor], List[List[str]]]]
    """Bags for each slide.

    This can either be a list of `.pt` files, a list of numpy arrays, a list
    of Tensors, or a list of lists of strings (where each item in the list is
    a patient, and nested items are slides for that patient).

    Each bag consists of features taken from all images from a slide. Data
    should be of shape N x F, where N is the number of instances and F is the
    number of features per instance/slide.
    """

    n_bags: int
    """Number of bags per instance."""

    bag_size: Optional[int] = None
    """The number of instances in each bag.
    For bags containing more instances, a random sample of `bag_size`
    instances will be drawn.  Smaller bags are padded with zeros.  If
    `bag_size` is None, all the samples will be used.
    """

    def __len__(self):
        return len(self.bags)

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, int]:

        bags = self.bags[index]
        assert len(bags) == self.n_bags

        # Load to tensors.
        loaded_bags = []
        for bag in bags:
            if isinstance(bag, str):
                loaded_bags.append(torch.load(bag).to(torch.float32))
            elif isinstance(self.bags[index], np.ndarray):
                loaded_bags.append(torch.from_numpy(bag))
            elif isinstance(self.bags[index], torch.Tensor):
                loaded_bags.append(bag)
            else:
                raise ValueError("Invalid bag type: {}".format(type(bag)))

        # Sample a subset, if required
        if self.bag_size:
            return [_to_fixed_size_bag(bag, bag_size=self.bag_size) for bag in loaded_bags]
        else:
            return [(bag, len(bag)) for bag in loaded_bags]


# -----------------------------------------------------------------------------

class MapDataset(Dataset):
    def __init__(
        self,
        func: Callable,
        *datasets: Union[npt.NDArray, Dataset],
        strict: bool = True
    ) -> None:
        """A dataset mapping over a function over other datasets.
        Args:
            func:  Function to apply to the underlying datasets.  Has to accept
                `len(dataset)` arguments.
            datasets:  The datasets to map over.
            strict:  Enforce the datasets to have the same length.  If
                false, then all datasets will be truncated to the shortest
                dataset's length.
        """
        if strict:
            assert all(len(ds) == len(datasets[0]) for ds in datasets)  # type: ignore
            self._len = len(datasets[0])  # type: ignore
        elif datasets:
            self._len = min(len(ds) for ds in datasets)  # type: ignore
        else:
            self._len = 0

        self._datasets = datasets
        self.func = func
        self.encoder = None

    def __len__(self) -> int:
        return self._len

    def __getitem__(self, index: int) -> Any:
        return self.func(*[ds[index] for ds in self._datasets])

    def new_empty(self):
        # FIXME hack to appease fastai's export
        return self

# -----------------------------------------------------------------------------

class SKLearnEncoder(Protocol):
    """An sklearn-style encoder."""

    categories_: List[List[str]]

    def transform(self, x: List[List[Any]]):
        ...


# -----------------------------------------------------------------------------

class EncodedDataset(MapDataset):
    """
    Wraps a single array of targets, optionally applying an sklearn-like encoder.
    """

    def __init__(self, encode: Optional[SKLearnEncoder], values: npt.NDArray):
        # If there's an encoder, we apply `_encode_item` to each target
        if encode is not None:
            super().__init__(self._encode_item, values)
        else:
            super().__init__(self._identity, values)
        self.encode = encode

    def _encode_item(self, y: Any) -> torch.Tensor:
        """
        Applies sklearn-like encoder (e.g. LabelEncoder, OneHotEncoder) to y,
        returning a float32 torch.Tensor.
        """
        # Convert to a numpy array (shape [1]) so that we can do transform(...)
        # If y is e.g. 2 -> transform => [[2]] -> one-hot => [[0,0,1,...]]
        arr = np.array([y])  # shape (1,)
        arr_2d = arr.reshape(-1, 1)  # shape (1,1)
        enc = self.encode.transform(arr_2d)  # shape (1, num_classes) for OneHotEncoder
        enc = torch.tensor(enc, dtype=torch.float32).squeeze(0) # Shape (num_classes)
        return enc

    def _identity(self, y: Any) -> torch.Tensor:
        """
        If no encoder is given, we just cast the label to float32 tensor.
        E.g., classification label = 2 => tensor([2.0])
        """
        if isinstance(y, torch.Tensor):
            return y.float()
        return torch.tensor([float(y)], dtype=torch.float32)