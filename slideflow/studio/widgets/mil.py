import imgui
import numpy as np
import slideflow as sf
import slideflow.mil
import threading
import traceback

from array import array
from tkinter.filedialog import askdirectory
from os.path import join, exists, dirname, abspath
from typing import Dict, Optional, List, Union
from slideflow.util import isnumeric
from slideflow.mil._params import ModelConfigCLAM, TrainerConfigCLAM
from slideflow.mil.eval import _predict_clam, _predict_mil

from ._utils import Widget
from .model import draw_tile_predictions
from ..gui import imgui_utils
from ..gui.viewer import SlideViewer
from ..utils import prediction_to_string
from .._mil_renderer import MILRenderer

# -----------------------------------------------------------------------------

RED = (1, 0, 0, 1)
GREEN = (0, 1, 0, 1)
YELLOW = (1, 1, 0, 1)
GRAY = (0.5, 0.5, 0.5, 1)

# -----------------------------------------------------------------------------


def _is_mil_model(path: str) -> bool:
    """Check if a given path is a valid MIL model."""
    return (exists(join(path, 'mil_params.json'))
            or (path.endswith('.pth')
                and dirname(path).endswith('models'))
                and exists(join(dirname(path), '../mil_params.json')))


def _get_mil_params(path: str) -> Dict:
    return sf.util.load_json(join(path, 'mil_params.json'))


def _draw_imgui_info(rows, viz):
    for y, cols in enumerate(rows):
        for x, col in enumerate(cols):
            col = str(col)
            if x != 0:
                imgui.same_line(viz.font_size * (6 + (x - 1) * 6))
            if x == 0:
                imgui.text_colored(col, *viz.theme.dim)
            else:
                with imgui_utils.clipped_with_tooltip(col, 22):
                    imgui.text(imgui_utils.ellipsis_clip(col, 22))

def _reshape_as_heatmap(
    predictions: np.ndarray,
    unmasked_indices: Union[np.ndarray, List[int]],
    original_shape: List[int],
    total_elements: int
) -> np.ndarray:
    heatmap = np.full(total_elements, np.nan, dtype=predictions.dtype)
    heatmap[unmasked_indices] = predictions

    # Reshape and normalize
    heatmap = heatmap.reshape(original_shape[:2])
    heatmap = (heatmap - np.nanmin(heatmap)) / (np.nanmax(heatmap) - np.nanmin(heatmap))
    return heatmap

class _AttentionHeatmapWrapper:

    def __init__(self, attention: np.ndarray, slide: "sf.WSI"):
        self.attention = attention
        self.slide = slide

    def save_npz(self, path: Optional[str] = None) -> str:
        """Save heatmap predictions and uncertainty in .npz format.

        Saves heatmap predictions to ``'predictions'`` in the .npz file. If uncertainty
        was calculated, this is saved to ``'uncertainty'``. A Heatmap instance can
        load a saved .npz file with :meth:`slideflow.Heatmap.load()`.

        Args:
            path (str, optional): Destination filename for .npz file. Defaults
                to {slidename}.npz

        Returns:
            str: Path to .npz file.
        """
        if path is None:
            path = f'{self.slide.name}.npz'
        np.savez(path, predictions=self.attention)
        return path

    def load(self):
        raise NotImplementedError("Not yet implemented.")

# -----------------------------------------------------------------------------

class MILWidget(Widget):

    tag = 'mil'
    description = 'Multiple-Instance Learning'
    icon = join(dirname(abspath(__file__)), '..', 'gui', 'buttons', 'button_mil.png')
    icon_highlighted = join(dirname(abspath(__file__)), '..', 'gui', 'buttons', 'button_mil_highlighted.png')

    def __init__(self, viz):
        self.viz = viz
        self._clicking      = None
        self._initialize_variables()
        self.mil_renderer = MILRenderer()
        self.viz.mil_widget = self  #TODO: hacky, remove this

    # --- Hooks, triggers, and internal functions -----------------------------

    def _initialize_variables(self):
        # Extractor, model, and config.
        self.mil_config = None
        self.mil_params = None
        self.extractor_params = None
        self.calculate_attention = True

        # Predictions and attention.
        self.predictions = None
        self.attention = None

        # Internals.
        self._mil_path = None
        self._show_mil_params = None
        self._rendering_message = "Generating whole-slide prediction..."
        self._generating = False
        self._triggered = False
        self._thread = None
        self._toast = None
        self._show_popup = False

    def _reload_wsi(self):
        """Reload a slide."""
        viz = self.viz
        if viz.wsi:
            viz.tile_px = self.extractor_params['tile_px']
            viz.tile_um = self.extractor_params['tile_um']
            viz.slide_widget.load(viz.wsi.path, mpp=viz.slide_widget.manual_mpp)

    def _refresh_generating_prediction(self):
        """Refresh render of asynchronous MIL prediction / attention heatmap."""
        if self._thread is not None and not self._thread.is_alive():
            self._generating = False
            self._triggered = False
            self._thread = None
            self.viz.clear_message(self._rendering_message)
            if self._toast is not None:
                self._toast.done()
                self._toast = None
            self.viz.create_toast("Prediction complete.", icon='success')

    def _before_model_load(self):
        """Trigger for when the user loads a tile-based model."""
        self.close()

    def drag_and_drop_hook(self, path: str) -> bool:
        """Drag-and-drop hook for loading an MIL model."""
        if _is_mil_model(path):
            return self.load(path)
        return False

    def open_menu_options(self) -> None:
        """Show a 'Load MIL Model' option in the File menu."""
        if imgui.menu_item('Load MIL Model...')[1]:
            self.ask_load_model()

    # --- Public API ----------------------------------------------------------

    @property
    def extractor(self):
        """Return the extractor used by the MIL model."""
        if self.viz._render_manager.is_async:
            raise RuntimeError("Cannot access MIL feature extractor while rendering in asynchronous mode.")
        renderer = self.viz.get_renderer()
        if isinstance(renderer, MILRenderer):
            return renderer.extractor
        else:
            return None

    @property
    def model(self):
        """Return the extractor used by the MIL model."""
        if self.viz._render_manager.is_async:
            raise RuntimeError("Cannot access MIL model while rendering in asynchronous mode.")
        renderer = self.viz.get_renderer()
        if isinstance(renderer, MILRenderer):
            return renderer.mil_model
        else:
            return None

    @property
    def normalizer(self):
        """Return the extractor used by the MIL model."""
        if self.viz._render_manager.is_async:
            raise RuntimeError("Cannot access MIL normalizer while rendering in asynchronous mode.")
        renderer = self.viz.get_renderer()
        if isinstance(renderer, MILRenderer):
            return renderer.normalizer
        else:
            return None

    @property
    def model_loaded(self):
        """Return True if a MIL model is loaded."""
        return self.mil_params is not None

    def close(self, close_renderer: bool = True):
        """Close the loaded MIL model."""
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        if self._mil_path == self.viz._model_path:
            self.viz._model_path = None
        self.viz.heatmap_widget.reset()
        self.viz.clear_prediction_message()
        self._initialize_variables()
        if self.viz._render_manager is not None:
            if close_renderer:
                self.viz._render_manager.close_renderer()

    def ask_load_model(self) -> None:
        """Prompt the user to open an MIL model."""
        mil_path = askdirectory(title="Load MIL Model (directory)...")
        if mil_path:
            self.load(mil_path)

    def load(self, path: str, allow_errors: bool = True) -> bool:
        try:
            self.close(close_renderer=False)
            self.mil_params = _get_mil_params(path)
            self.extractor_params = self.mil_params['bags_extractor']
            self._reload_wsi()
            self.mil_config = sf.mil.mil_config(trainer=self.mil_params['trainer'],
                                                **self.mil_params['params'])
            self.viz.close_model(True)  # Close a tile-based model, if one is loaded
            self.viz.tile_um = self.extractor_params['tile_um']
            self.viz.tile_px = self.extractor_params['tile_px']

            if self.viz.viewer and not isinstance(self.viz.viewer, SlideViewer):
                self.viz.viewer.set_tile_px(self.viz.tile_px)
                self.viz.viewer.set_tile_um(self.viz.tile_um)

            # Add MIL renderer to the render pipeline.
            self.viz._render_manager.set_renderer(MILRenderer, mil_model_path=path)

            self.viz._model_path = path
            self._mil_path = path
            self.viz.create_toast('MIL model loaded', icon='success')
        except Exception as e:
            if allow_errors:
                self.viz.create_toast('Error loading MIL model', icon='error')
                sf.log.error(e)
                sf.log.error(traceback.format_exc())
                return False
            raise e
        return True

    def _calculate_predictions(self, bags):
        """Calculate MIL predictions and attention from a set of bags."""
        if (isinstance(self.mil_config, TrainerConfigCLAM)
        or isinstance(self.mil_config.model_config, ModelConfigCLAM)):
            predictions, attention = _predict_clam(
                self.model,
                bags,
                attention=self.calculate_attention,
                device=self.viz._render_manager.device
            )
        else:
            predictions, attention = _predict_mil(
                self.model,
                bags,
                attention=self.calculate_attention,
                use_lens=self.mil_config.model_config.use_lens,
                apply_softmax=self.mil_config.model_config.apply_softmax,
                device=self.viz._render_manager.device
            )
        return predictions, attention

    def _predict_slide(self):
        viz = self.viz

        self._generating = True
        self._triggered = True

        # Generate features with the loaded extractor.
        masked_bags = self.extractor(
            viz.wsi,
            normalizer=self.normalizer,
            **viz.slide_widget.get_tile_filter_params(),
        )
        original_shape = masked_bags.shape
        masked_bags = masked_bags.reshape((-1, masked_bags.shape[-1]))
        mask = masked_bags.mask.any(axis=1)
        valid_indices = np.where(~mask)
        bags = masked_bags[valid_indices]
        bags = np.expand_dims(bags, axis=0).astype(np.float32)

        sf.log.info("Generated feature bags for {} tiles".format(bags.shape[1]))

        # Generate slide-level prediction and attention.
        self.predictions, self.attention = self._calculate_predictions(bags)
        if self.attention:
            self.attention = self.attention[0]
        else:
            self.attention = None

        # Generate tile-level predictions.
        # Reshape the bags from (1, n_bags, n_feats) to (n_bags, 1, n_feats)
        reshaped_bags = np.reshape(bags, (bags.shape[1], 1, bags.shape[2]))
        tile_predictions, _ = self._calculate_predictions(reshaped_bags)

        # Create heatmaps from tile predictions and attention
        if len(tile_predictions.shape) == 2:
            tile_heatmap = np.stack([
                _reshape_as_heatmap(tile_predictions[:, n], valid_indices, original_shape, masked_bags.shape[0])
                for n in range(tile_predictions.shape[1])
            ], axis=2)
        else:
            tile_heatmap = _reshape_as_heatmap(
                tile_predictions, valid_indices, original_shape, masked_bags.shape[0]
            )
        if self.attention is not None:
            att_heatmap = _reshape_as_heatmap(
                self.attention, valid_indices, original_shape, masked_bags.shape[0]
            )
            self.render_dual_heatmap(att_heatmap, tile_heatmap)
        else:
            self.render_tile_prediction_heatmap(tile_heatmap)

    def predict_slide(self):
        """Initiate a whole-slide prediction."""
        if not self.verify_tile_size():
            return
        self.viz.set_message(self._rendering_message)
        self._toast = self.viz.create_toast(
            title="Generating prediction",
            sticky=True,
            spinner=True,
            icon='info'
        )
        self._thread = threading.Thread(target=self._predict_slide)
        self._thread.start()

    def verify_tile_size(self) -> bool:
        """Verify that the current slide matches the MIL model's tile size."""
        viz = self.viz
        mil_tile_um = self.extractor_params['tile_um']
        mil_tile_px = self.extractor_params['tile_px']
        if viz.wsi.tile_px != mil_tile_px or viz.wsi.tile_um != mil_tile_um:
            viz.create_toast(
                "MIL model tile size (tile_px={}, tile_um={}) does not match "
                "the currently loaded slide (tile_px={}, tile_um={}).".format(
                    mil_tile_px, mil_tile_um, viz.wsi.tile_px, viz.wsi.tile_um
                ),
                icon='error'
            )
            return False
        return True

    def is_categorical(self) -> bool:
        return (('model_type' not in self.mil_params)
                or (self.mil_params['model_type'] == 'categorical'))

    def render_attention_heatmap(self, attention: np.ndarray) -> None:
        self.viz.heatmap = _AttentionHeatmapWrapper(array, self.viz.wsi)
        self.viz.heatmap_widget.predictions = array[:, :, np.newaxis]
        self.viz.heatmap_widget.render_heatmap(outcome_names=["Attention"])

    def render_tile_prediction_heatmap(self, tile_preds: np.ndarray) -> None:
        if len(tile_preds.shape) == 2:
            tile_preds = tile_preds[:, :, np.newaxis]
        self.viz.heatmap_widget.predictions = tile_preds
        self.viz.heatmap_widget.render_heatmap(
            outcome_names=self.viz.heatmap_widget._get_all_outcome_names(self.mil_params)
        )

    def render_dual_heatmap(self, attention: np.ndarray, tile_preds: np.ndarray) -> None:
        if tile_preds.shape[0:2] != attention.shape[0:2]:
            raise ValueError("Attention and tile_preds must have the same shape.")
        if len(tile_preds.shape) == 2:
            tile_preds = tile_preds[:, :, np.newaxis]

        self.viz.heatmap = _AttentionHeatmapWrapper(array, self.viz.wsi)
        self.viz.heatmap_widget.predictions = np.concatenate(
            (attention[:, :, np.newaxis], tile_preds),
            axis=2
        )
        pred_outcomes = self.viz.heatmap_widget._get_all_outcome_names(self.mil_params)
        self.viz.heatmap_widget.render_heatmap(outcome_names=["Attention"] + pred_outcomes)

    def draw_extractor_info(self):
        """Draw a description of the extractor information."""

        viz = self.viz
        if self.extractor_params is None:
            imgui.text("No extractor loaded.")
            return
        c = self.extractor_params

        if 'normalizer' in c and c['normalizer']:
            normalizer = c['normalizer']['method']
        else:
            normalizer = "-"

        rows = [
            ['Extractor',         c['extractor']['class'].split('.')[-1]],
            ['Extractor Args',    c['extractor']['kwargs']],
            ['Normalizer',      normalizer],
            ['Num features',    c['num_features']],
            ['Tile size (px)',  c['tile_px']],
            ['Tile size (um)',  c['tile_um']],
        ]
        _draw_imgui_info(rows, viz)
        imgui_utils.vertical_break()

    def draw_mil_info(self):
        """Draw a description of the MIL model."""

        viz = self.viz
        if self.mil_params is None:
            imgui.text("No MIL model loaded.")
            return
        c = self.mil_params

        rows = [
            ['Outcomes',      c['outcomes']],
            ['Input size',    c['input_shape']],
            ['Output size',   c['output_shape']],
            ['Trainer',       c['trainer']],
        ]
        _draw_imgui_info(rows, viz)

        # MIL model params button and popup.
        with imgui_utils.grayed_out('params' not in c):
            imgui.same_line(imgui.get_content_region_max()[0] - viz.font_size - viz.spacing * 2)
            if imgui.button("HP") and 'params' in c:
                self._show_mil_params = not self._show_mil_params

    def draw_mil_params_popup(self):
        """Draw popup showing MIL model hyperparameters."""

        viz = self.viz
        hp = self.mil_params['params']
        rows = list(zip(list(map(str, hp.keys())), list(map(str, hp.values()))))

        _, self._show_mil_params = imgui.begin("MIL parameters", closable=True, flags=imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_SCROLLBAR)
        for y, cols in enumerate(rows):
            for x, col in enumerate(cols):
                if x != 0:
                    imgui.same_line(viz.font_size * 10)
                if x == 0:
                    imgui.text_colored(col, *viz.theme.dim)
                else:
                    imgui.text(col)
        imgui.end()

    def draw_prediction(self):
        """Draw the final prediction."""
        if self.predictions is None:
            return
        assert len(self.predictions) == 1
        prediction = self.predictions[0]

        # Assemble outcome category labels.
        outcome_labels = [
            f"Outcome {i}" if 'outcome_labels' not in self.mil_params or str(i) not in self.mil_params['outcome_labels']
                           else self.mil_params['outcome_labels'][str(i)]
            for i in range(len(prediction))
        ]

        # Show prediction for each category.
        imgui.text(self.mil_params['outcomes'])
        imgui.separator()
        for i, pred_val in enumerate(prediction):
            imgui.text_colored(outcome_labels[i], *self.viz.theme.dim)
            imgui.same_line(self.viz.font_size * 12)
            imgui_utils.right_aligned_text(f"{pred_val:.3f}")
        imgui.separator()
        # Show final prediction based on which category has the highest probability.
        imgui.text("Final prediction")
        imgui.same_line(self.viz.font_size * 12)
        imgui_utils.right_aligned_text(f"{outcome_labels[np.argmax(prediction)]}")

    def draw_config_popup(self):
        viz = self.viz

        if self._show_popup:
            cx, cy = imgui.get_cursor_pos()
            imgui.set_next_window_position(viz.sidebar.full_width, cy)
            imgui.begin(
                '##mil_popup',
                flags=(imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE)
            )
            if imgui.menu_item('Load MIL model')[0]:
                self.ask_load_model()
            if imgui.menu_item('Close MIL model')[0]:
                self.close()

            # Hide menu if we click elsewhere
            if imgui.is_mouse_down(0) and not imgui.is_window_hovered():
                self._clicking = True
            if self._clicking and imgui.is_mouse_released(0):
                self._clicking = False
                self._show_popup = False

            imgui.end()

    def update_attention_color(self):
        viz = self.viz
        val = viz._uncertainty
        c = self.mil_params

        if not self.model_loaded:
            return

        # DEFAULT
        color = GRAY

        # Out of focus.
        if hasattr(viz.result, 'in_focus') and not viz.result.in_focus:
            color = GRAY

        # Has thresholds.
        elif isnumeric(val):
            if 'thresholds' in c and 'attention' in c['thresholds']:
                thresh = c['thresholds']['attention']
                if 'low' in thresh and val < thresh['low']:
                    color = RED
                elif 'high' in thresh and val > thresh['high']:
                    color = GREEN
                elif 'low' in thresh and 'high' in thresh:
                    color = YELLOW
                else:
                    color = GRAY

        if ('thresholds' in c
            and 'attention' in c['thresholds']
            and 'range' in c['thresholds']['attention']):
            self.uncertainty_range = c['thresholds']['attention']['range']
        else:
            self.uncertainty_range = None

        self.uncertainty_color = color
        viz._box_color = color[0:3]

    @imgui_utils.scoped_by_object_id
    def __call__(self, show=True):
        viz = self.viz

        if self._generating:
            self._refresh_generating_prediction()

        self.update_attention_color()

        if show:
            with viz.header_with_buttons("Multiple-Instance Learning"):
                imgui.same_line(imgui.get_content_region_max()[0] - viz.font_size*1.5)
                cx, cy = imgui.get_cursor_pos()
                imgui.set_cursor_position((cx, cy-int(viz.font_size*0.25)))
                if viz.sidebar.small_button('gear'):
                    self._clicking = False
                    self._show_popup = not self._show_popup
                self.draw_config_popup()

        if show and self.model_loaded:
            if viz.collapsing_header('Feature Extractor', default=True):
                self.draw_extractor_info()
            if viz.collapsing_header('MIL Model', default=True):
                self.draw_mil_info()
            if viz.collapsing_header('Whole-slide Prediction', default=True):
                self.draw_prediction()
                predict_enabled = (viz.wsi is not None
                                   and self.model_loaded
                                   and not self._triggered)
                predict_text = "Predict Slide" if not self._triggered else f"Calculating{imgui_utils.spinner_text()}"
                if viz.sidebar.full_button(predict_text, enabled=predict_enabled):
                    self.predict_slide()
            if viz.collapsing_header('Tile Prediction', default=True):
                draw_tile_predictions(
                    viz,
                    is_categorical=self.is_categorical(),
                    config=self.mil_params,
                    has_preds=(viz._predictions is not None),
                    using_model=self.model_loaded,
                    uncertainty_color=self.uncertainty_color,
                    uncertainty_range=self.uncertainty_range,
                    uncertainty_label="Attention",
                )
        elif show:
            imgui_utils.padded_text('No MIL model has been loaded.', vpad=[int(viz.font_size/2), int(viz.font_size)])
            if viz.sidebar.full_button("Load an MIL Model"):
                self.ask_load_model()

        if self._show_mil_params and self.mil_params:
            self.draw_mil_params_popup()

        if (viz._predictions is not None) and self.model_loaded:
            pred_str = prediction_to_string(
                predictions=viz._predictions,
                outcomes=self.mil_params['outcome_labels'],
                is_categorical=self.is_categorical()
            )
            viz.set_prediction_message(pred_str)
