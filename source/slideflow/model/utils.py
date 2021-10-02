"""Backend-agnostic model utility functions."""
import tensorflow as tf
import os
import csv
import tempfile
import numpy as np
import slideflow as sf
from slideflow.util import log

class HyperParameterError(Exception):
    pass

def to_onehot(val, max):
    onehot = np.zeros(max, dtype=np.int64)
    onehot[val] = 1
    return onehot

def get_hp_from_batch_file(batch_train_file, models=None):
    """Organizes a list of hyperparameters ojects and associated models names.

    Args:
        batch_train_file (str): Path to train train TSV file.
        models (list(str)): List of model names. Defaults to None.
            If not supplied, returns all valid models from batch file.

    Returns:
        List of (Hyperparameter, model_name) for each HP combination
    """

    if models is not None and not isinstance(models, list):
        raise sf.util.UserError("If supplying models, must be a list of strings containing model names.")
    if isinstance(models, list) and not list(set(models)) == models:
        raise sf.util.UserError("Duplicate model names provided.")

    # First, ensure all indicated models are in the batch train file
    if models:
        valid_models = []
        with open(batch_train_file) as csv_file:
            reader = csv.reader(csv_file, delimiter='\t')
            header = next(reader)
            try:
                model_name_i = header.index('model_name')
            except:
                err_msg = "Unable to find column 'model_name' in the batch training config file."
                log.error(err_msg)
                raise ValueError(err_msg)
            for row in reader:
                model_name = row[model_name_i]
                # First check if this row is a valid model
                if (not models) or (isinstance(models, str) and model_name==models) or model_name in models:
                    # Now verify there are no duplicate model names
                    if model_name in valid_models:
                        err_msg = f'Duplicate model names found in {sf.util.green(batch_train_file)}.'
                        log.error(err_msg)
                        raise ValueError(err_msg)
                    valid_models += [model_name]
        missing_models = [m for m in models if m not in valid_models]
        if missing_models:
            raise ValueError(f"Unable to find the following models in the batch train file: {', '.join(missing_models)}")

    # Read the batch train file and generate HyperParameter objects from the given configurations
    hyperparameters = {}
    batch_train_rows = []
    with open(batch_train_file) as csv_file:
        reader = csv.reader(csv_file, delimiter='\t')
        header = next(reader)
        for row in reader:
            batch_train_rows += [row]

    for row in batch_train_rows:
        try:
            hp, hp_model_name = get_hp_from_row(row, header)
        except HyperParameterError as e:
            log.error('Invalid Hyperparameter combination: ' + str(e))
            return
        if models and hp_model_name not in models: continue
        hyperparameters[hp_model_name] = hp
    return hyperparameters

def get_hp_from_row(row, header):
    """Converts a row in the batch_train CSV file into a HyperParameters object."""

    from slideflow.model import HyperParameters

    model_name_i = header.index('model_name')
    args = header[0:model_name_i] + header[model_name_i+1:]
    model_name = row[model_name_i]
    hp = HyperParameters()
    for arg in args:
        value = row[header.index(arg)]
        if arg in hp._get_args():
            if arg != 'epochs':
                arg_type = type(getattr(hp, arg))
                if arg_type == bool:
                    if value.lower() in ['true', 'yes', 'y', 't']:
                        bool_val = True
                    elif value.lower() in ['false', 'no', 'n', 'f']:
                        bool_val = False
                    else:
                        raise ValueError(f'Unable to parse "{value}" for batch file argument "{arg}" into a bool.')
                    setattr(hp, arg, bool_val)
                elif arg in ('L2_weight', 'dropout'):
                    setattr(hp, arg, float(value))
                else:
                    setattr(hp, arg, arg_type(value))
            else:
                epochs = [int(i) for i in value.translate(str.maketrans({'[':'', ']':''})).split(',')]
                setattr(hp, arg, epochs)
        else:
            log.error(f"Unknown argument '{arg}' found in training config file.", 0)
    return hp, model_name