"""Utility functions and sample configuration spaces
for Bayesian hyperparameter searching with SMAC."""

import ConfigSpace.hyperparameters as cs_hp
from typing import Optional, List, Union, Tuple
from ConfigSpace import (
    ConfigurationSpace, NotEqualsCondition, LessThanCondition
)

def broad_search_space(
    *,
    batch_size: Optional[List[int]] = [8, 16, 32, 64],                  # Ordinal
    dropout: Optional[Tuple[float, float]] = (0, 0.5),                  # Float (range)
    l1: Optional[Tuple[float, float]] = (0, 0.5),                       # Float (range)
    l2: Optional[Tuple[float, float]] = (0, 0.5),                       # Float (range)
    l1_dense: Optional[Tuple[float, float]] = (0, 0.5),                 # Float (range)
    l2_dense: Optional[Tuple[float, float]] = (0, 0.5),                 # Float (range)
    learning_rate: Optional[Tuple[float, float]] = (0.00001, 0.001),    # Float (log range)
    learning_rate_decay: Optional[Tuple[float, float]] = (0.95, 1),     # Float (range)
    learning_rate_decay_steps: Optional[Tuple[int, int]] = (128, 1024), # Int (log range)
    hidden_layers: Optional[List[int]] = [0, 1, 2, 3],                  # Int (ordinal)
    hidden_layer_width: Optional[List[int]] = [64, 128, 256, 512],      # Int (ordinal)
    **kwargs
) -> ConfigurationSpace:
    """Build a configuration space for a broad hyperparameter search."""
    return create_search_space(
        batch_size=batch_size,
        dropout=dropout,
        l1=l1,
        l2=l2,
        l1_dense=l1_dense,
        l2_dense=l2_dense,
        learning_rate=learning_rate,
        learning_rate_decay=learning_rate_decay,
        learning_rate_decay_steps=learning_rate_decay_steps,
        hidden_layers=hidden_layers,
        hidden_layer_width=hidden_layer_width,
        **kwargs
    )

def shallow_search_space(
    *,
    batch_size: Optional[List[int]] = [8, 16, 32, 64],                  # Ordinal
    dropout: Optional[Tuple[float, float]] = (0, 0.3),                  # Float (range)
    l2: Optional[Tuple[float, float]] = (0, 0.3),                       # Float (range)
    l2_dense: Optional[Tuple[float, float]] = (0, 0.3),                 # Float (range)
    learning_rate: Optional[Tuple[float, float]] = (0.00005, 0.0005),   # Float (log range)
    hidden_layers: Optional[List[int]] = [0, 1, 2],                     # Int (ordinal)
    **kwargs
) -> ConfigurationSpace:
    """Build a configuration space for a shallow hyperparameter search."""
    return create_search_space(
        batch_size=batch_size,
        dropout=dropout,
        l2=l2,
        l2_dense=l2_dense,
        learning_rate=learning_rate,
        hidden_layers=hidden_layers,
        **kwargs
    )

def create_search_space(
    *,
    # Preprocessing hyperparameters
    tile_px: Optional[List[int]] = None,                          # Ordinal
    tile_um: Optional[List[Union[int, str]]] = None,              # Ordinal or categorical
    augment: Optional[List[str]] = None,                          # Categorical
    normalizer: Optional[List[str]] = None,                       # Categorical
    normalizer_source: Optional[List[str]] = None,                # Categorical
    # Model hyperparameters
    model: Optional[List[str]] = None,                            # Categorical
    # Training hyperparameters
    batch_size: Optional[List[int]] = None,                       # Int (ordinal)
    dropout: Optional[Tuple[float, float]] = None,                # Float (range)
    l1: Optional[Tuple[float, float]] = None,                     # Float (range)
    l2: Optional[Tuple[float, float]] = None,                     # Float (range)
    l1_dense: Optional[Tuple[float, float]] = None,               # Float (range)
    l2_dense: Optional[Tuple[float, float]] = None,               # Float (range)
    learning_rate: Optional[Tuple[float, float]] = None,          # Float (log range)
    learning_rate_decay: Optional[Tuple[float, float]] = None,    # Float (range)
    learning_rate_decay_steps: Optional[Tuple[int, int]] = None,  # Int (log range)
    hidden_layers: Optional[List[int]] = None,                    # Int (ordinal)
    hidden_layer_width: Optional[List[int]] = None,               # Int (ordinal)
    pooling: Optional[List[str]] = None,                          # Categorical
    trainable_layers: Optional[Tuple[int, int]] = None,           # Int (range)
    early_stop: bool = False,                                     # Boolean
) -> ConfigurationSpace:
    """Build a configuration space for a hyperparameter search."""

    cs = ConfigurationSpace()

    # --- Pre-processing ------------------------------------------------------
    if tile_px is not None:
        assert isinstance(tile_px, list)
        if all(isinstance(t, (int, float)) for t in tile_px):
            tile_px = sorted(tile_px)
            cs.add_hyperparameter(cs_hp.OrdinalHyperparameter("tile_px", tile_px, default_value=tile_px[0]))
        else:
            raise ValueError('Invalid values encountered in parameter "tile_px"')
    if tile_um is not None:
        assert isinstance(tile_um, list)
        if all(isinstance(t, (int, float)) for t in tile_um):
            tile_um = sorted(tile_um)
            cs.add_hyperparameter(cs_hp.OrdinalHyperparameter("tile_um", tile_um, default_value=tile_um[0]))
        else:
            cs.add_hyperparameter(cs_hp.CategoricalHyperparameter("tile_um", tile_um, default_value=tile_um[0]))
    if augment is not None:
        assert isinstance(augment, list)
        cs.add_hyperparameter(cs_hp.CategoricalHyperparameter("augment", augment, default_value=augment[0]))
    if normalizer is not None:
        assert isinstance(normalizer, list)
        cs.add_hyperparameter(cs_hp.CategoricalHyperparameter("normalizer", normalizer, default_value=normalizer[0]))
    if normalizer_source is not None:
        assert isinstance(normalizer_source, list)
        cs.add_hyperparameter(cs_hp.CategoricalHyperparameter("normalizer_source", normalizer_source, default_value=normalizer_source[0]))

    # --- Model/architecture hyperparameters ----------------------------------
    if model is not None:
        assert isinstance(model, list)
        cs.add_hyperparameter(cs_hp.CategoricalHyperparameter("model", model, default_value=model[0]))

    # --- Training hyperparameters --------------------------------------------
    if batch_size is not None:
        assert isinstance(batch_size, list)
        assert all([isinstance(b, int) for b in batch_size])
        batch_size = sorted(batch_size)
        cs.add_hyperparameter(cs_hp.OrdinalHyperparameter("batch_size", batch_size, default_value=batch_size[0]))
    if dropout is not None:
        assert isinstance(dropout, (list, tuple)) and len(dropout) == 2
        cs.add_hyperparameter(cs_hp.UniformFloatHyperparameter("dropout", dropout[0], dropout[1]))
    if l1 is not None:
        assert isinstance(l1, (list, tuple)) and len(l1) == 2
        cs.add_hyperparameter(cs_hp.UniformFloatHyperparameter("l1", l1[0], l1[1]))
    if l2 is not None:
        assert isinstance(l2, (list, tuple)) and len(l2) == 2
        cs.add_hyperparameter(cs_hp.UniformFloatHyperparameter("l2", l2[0], l2[1]))
    if l1_dense is not None:
        assert isinstance(l1_dense, (list, tuple)) and len(l1_dense) == 2
        cs.add_hyperparameter(cs_hp.UniformFloatHyperparameter("l1_dense", l1_dense[0], l1_dense[1]))
    if l2_dense is not None:
        assert isinstance(l2_dense, (list, tuple)) and len(l2_dense) == 2
        cs.add_hyperparameter(cs_hp.UniformFloatHyperparameter("l2_dense", l2_dense[0], l2_dense[1]))
    if learning_rate is not None:
        assert isinstance(learning_rate, (list, tuple)) and len(learning_rate) == 2
        cs.add_hyperparameter(cs_hp.UniformFloatHyperparameter("learning_rate", learning_rate[0], learning_rate[1], log=True))
    if learning_rate_decay is not None:
        assert isinstance(learning_rate_decay, (list, tuple)) and len(learning_rate_decay) == 2
        decay = cs_hp.UniformFloatHyperparameter("learning_rate_decay", learning_rate_decay[0], learning_rate_decay[1])
        cs.add_hyperparameter(decay)
    if learning_rate_decay_steps is not None:
        assert isinstance(learning_rate_decay_steps, (list, tuple)) and len(learning_rate_decay_steps) == 2
        lr_start, lr_end = learning_rate_decay_steps
        decay_steps = cs_hp.UniformIntegerHyperparameter("learning_rate_decay_steps", lr_start, lr_end, log=True)
        cs.add_hyperparameter(decay_steps)
    if hidden_layers is not None:
        assert isinstance(hidden_layers, (list, tuple))
        assert all([isinstance(b, int) for b in hidden_layers])
        hidden_layers = sorted(hidden_layers)
        hl = cs_hp.OrdinalHyperparameter("hidden_layers", hidden_layers, default_value=hidden_layers[0])
        cs.add_hyperparameter(hl)
    if hidden_layer_width is not None:
        assert isinstance(hidden_layer_width, (list, tuple))
        assert all([isinstance(b, int) for b in hidden_layer_width])
        hidden_layer_width = sorted(hidden_layer_width)
        hl_width = cs_hp.OrdinalHyperparameter("hidden_layer_width", hidden_layer_width, default_value=hidden_layer_width[0])
        cs.add_hyperparameter(hl_width)
    if pooling is not None:
        assert isinstance(pooling, list)
        cs.add_hyperparameter(cs_hp.CategoricalHyperparameter("pooling", pooling, default_value=pooling[0]))
    if trainable_layers is not None:
        assert isinstance(trainable_layers, (list, tuple)) and len(trainable_layers) == 2
        tl_start, tl_end = trainable_layers
        cs.add_hyperparameter(cs_hp.UniformIntegerHyperparameter("trainable_layers", tl_start, tl_end))
    if early_stop:
        cs_hp.CategoricalHyperparameter("early_stop", [True, False], default_value=False)

    # --- Conditions ----------------------------------------------------------
    # Only sample hyperparameter hidden_layer_width if hidden_layers > 0
    if hidden_layers is not None and hidden_layer_width is not None:
        cs.add_condition(NotEqualsCondition(hl_width, hl, 0))
    # Only sample learning_rate_decay_steps if learning_rate_decay < 1 (decay of 1 is no decay)
    if learning_rate_decay is not None and learning_rate_decay_steps is not None:
        cs.add_condition(LessThanCondition(decay_steps, decay, 1))
    # Do not sample l1_dense if hidden_layers = 0
    if hidden_layers is not None and l1_dense is not None:
        cs.add_condition(NotEqualsCondition(cs['l1_dense'], hl, 0))
    # Do not sample l2_dense if hidden_layers = 0
    if hidden_layers is not None and l2_dense is not None:
        cs.add_condition(NotEqualsCondition(cs['l2_dense'], hl, 0))

    return cs
