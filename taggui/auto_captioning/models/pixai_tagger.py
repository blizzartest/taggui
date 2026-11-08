from datetime import datetime

import numpy as np
import torch
import torch.nn.functional as F
from transformers import AutoModel
from transformers.utils.import_utils import is_torch_bf16_gpu_available

import auto_captioning.captioning_thread as captioning_thread
from auto_captioning.auto_captioning_model import AutoCaptioningModel
from auto_captioning.models.wd_tagger import (KAOMOJIS,
                                              get_tags_to_exclude)
from utils.enums import FrameAggregation
from utils.video import DEFAULT_MAX_VIDEO_FRAMES


def format_tag(tag: str) -> str:
    if tag not in KAOMOJIS:
        tag = tag.replace('_', ' ')
    return tag


class PixaiTaggerModel:
    def __init__(self, model_id: str, device: torch.device,
                 dtype: torch.dtype, pixai_tagger_settings: dict):
        model = AutoModel.from_pretrained(model_id, trust_remote_code=True,
                                          torch_dtype=dtype).to(device)
        model.eval()
        self.model = model
        self.device = device
        config = model.config
        self.tags = [format_tag(tag) for tag in config.tags]
        self.category_thresholds = config.category_best_threshold
        if self.category_thresholds is None:
            self.category_thresholds = {
                category: 0.2 for category in config.tags_split}
        # The index of the first tag of each category.
        self.category_start_indices = {}
        index = 0
        for category, count in config.tags_split:
            self.category_start_indices[category] = index
            index += count

    def get_category_thresholds(
            self, pixai_tagger_settings: dict) -> dict[str, float]:
        # A minimum probability of 0 means the model's recommended threshold
        # for the category is used.
        category_thresholds = {}
        for category in self.category_start_indices:
            if not pixai_tagger_settings[f'include_{category}_tags']:
                continue
            category_threshold = (pixai_tagger_settings[
                f'{category}_min_probability'])
            if category_threshold <= 0:
                category_threshold = self.category_thresholds[category]
            category_thresholds[category] = category_threshold
        return category_thresholds

    def get_probabilities_from_tensors(
            self, pixel_values: torch.Tensor) -> np.ndarray:
        """Run inference on preprocessed frames and return the raw sigmoid
        probabilities for all tags.

        `pixel_values` is a (N, C, H, W) float tensor on the model device;
        the return value has shape (N, tag count). The frames are split
        into slices to bound GPU memory use."""
        batch_parts = []
        max_frames_per_pass = 64
        model_dtype = next(self.model.parameters()).dtype
        for start in range(0, pixel_values.shape[0], max_frames_per_pass):
            frame_slice = pixel_values[start:start + max_frames_per_pass]
            frame_slice = frame_slice.to(model_dtype)
            with torch.inference_mode():
                probabilities = self.model(frame_slice).sigmoid()
            batch_parts.append(probabilities.cpu().numpy())
        return np.concatenate(batch_parts, axis=0)

    def aggregate_probabilities(
            self, all_probabilities: list[np.ndarray],
            aggregation: FrameAggregation) -> np.ndarray:
        """Aggregate per-frame probabilities into a single array."""
        stacked = np.stack(all_probabilities, axis=0)
        if aggregation == FrameAggregation.MAX:
            return stacked.max(axis=0)
        return stacked.mean(axis=0)

    def probabilities_to_tags(
            self, probabilities: np.ndarray,
            pixai_tagger_settings: dict) -> tuple[tuple, tuple]:
        """Apply category thresholds, exclusions, sorting, and the max-tags
        limit to an aggregated probability array."""
        tags_to_exclude = get_tags_to_exclude(
            pixai_tagger_settings['tags_to_exclude'])
        category_thresholds = self.get_category_thresholds(
            pixai_tagger_settings)
        tags_and_probabilities = []
        index = 0
        for category, count in self.model.config.tags_split:
            if category in category_thresholds:
                threshold = category_thresholds[category]
                for offset in range(count):
                    probability = float(probabilities[index + offset])
                    tag = self.tags[index + offset]
                    if (probability >= threshold
                            and tag not in tags_to_exclude):
                        tags_and_probabilities.append((tag, probability))
            index += count
        tags_and_probabilities.sort(key=lambda x: x[1], reverse=True)
        tags_and_probabilities = tags_and_probabilities[
            :pixai_tagger_settings['max_tags']]
        if tags_and_probabilities:
            tags, probabilities_tuple = zip(*tags_and_probabilities)
        else:
            tags, probabilities_tuple = (), ()
        return tags, probabilities_tuple


class PixaiTagger(AutoCaptioningModel):
    image_mode = 'RGBA'

    def __init__(self,
                 captioning_thread_: 'captioning_thread.CaptioningThread',
                 caption_settings: dict):
        super().__init__(captioning_thread_, caption_settings)
        # The tagger works best in float32 on CPU.
        if self.device.type == 'cpu':
            self.dtype = torch.float32
        self.pixai_tagger_settings = self.caption_settings[
            'pixai_tagger_settings']
        self.show_probabilities = self.pixai_tagger_settings[
            'show_probabilities']
        self.multiframe_enabled = self.pixai_tagger_settings.get(
            'multiframe_enabled', False)
        self.multiframe_frame_count = self.pixai_tagger_settings.get(
            'multiframe_frame_count', 5)
        # For videos, the frame count is treated as frames per interval and
        # scaled with the video duration.
        self.multiframe_frames_per_interval = self.pixai_tagger_settings.get(
            'multiframe_frames_per_interval', 12)
        self.multiframe_interval_seconds = self.pixai_tagger_settings.get(
            'multiframe_interval_seconds', 5.0)
        # Cap on the number of frames extracted from a single video; 0
        # means no cap.
        self.multiframe_video_max_frames = self.pixai_tagger_settings.get(
            'multiframe_video_max_frames', DEFAULT_MAX_VIDEO_FRAMES)
        self.multiframe_aggregation = FrameAggregation(
            self.pixai_tagger_settings.get(
                'multiframe_aggregation', FrameAggregation.MEAN.value))
        self.batch_size = self.pixai_tagger_settings.get('batch_size', 1)
        if (self.pixai_tagger_settings.get('use_bfloat16', False)
                and self.device.type == 'cuda'
                and is_torch_bf16_gpu_available()):
            self.dtype = torch.bfloat16
        if self.device.type == 'cuda':
            # TF32 speeds up convolutions and matrix multiplications on
            # Ampere and newer GPUs with a negligible effect on accuracy.
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        # Image loading and preprocessing only uses the CPU and does not
        # touch the model, so it can run in prefetch worker threads.
        self.supports_prefetch = True

    def get_error_message(self) -> str | None:
        return None

    def get_processor(self):
        return None

    def get_model(self):
        return PixaiTaggerModel(self.model_id, self.device, self.dtype,
                                self.pixai_tagger_settings)

    def get_captioning_message(self, are_multiple_images_selected: bool,
                               captioning_start_datetime: datetime) -> str:
        device_description = self.get_device_description()
        if are_multiple_images_selected:
            captioning_start_datetime_string = (
                self.get_captioning_start_datetime_string(
                    captioning_start_datetime))
            return (f'Generating tags... (device: {device_description}, '
                    f'start time: {captioning_start_datetime_string})')
        return f'Generating tags... (device: {device_description})'

    def get_model_inputs(self, image_prompt: str,
                         image: 'captioning_thread.Image') -> np.ndarray:
        """Decode the image (or frames) and return the raw RGBA pixel
        arrays. Preprocessing runs on the GPU afterwards."""
        if self.multiframe_enabled:
            frames = self.load_frames(
                image, self.multiframe_frame_count,
                frames_per_interval=self.multiframe_frames_per_interval,
                interval_seconds=self.multiframe_interval_seconds,
                max_frames=self.multiframe_video_max_frames)
            if frames:
                if len(frames) > 1:
                    return np.stack([np.array(frame) for frame in frames])
                return np.array(frames[0])
        pil_image = self.load_image(image)
        return np.array(pil_image)

    def preprocess_frames(self, frame_arrays: np.ndarray) -> torch.Tensor:
        """Preprocess raw RGBA frames on the model device: composite
        transparency over white, resize while preserving the aspect ratio,
        pad to a square, and normalize.

        `frame_arrays` has shape (N, H, W, 4) with uint8 values; the return
        value is a (N, 3, S, S) float32 tensor on the model device."""
        input_size = self.model.model.config.img_size
        frames = []
        for frame_array in frame_arrays:
            frame = torch.from_numpy(frame_array).to(self.device)
            frame = frame.permute(2, 0, 1).float().div(255)
            rgb = frame[:3]
            alpha = frame[3]
            # Composite the image over a white background in case it has
            # transparent areas.
            rgb = rgb * alpha + (1 - alpha)
            height, width = rgb.shape[-2:]
            scale = input_size / max(height, width)
            new_height = round(height * scale)
            new_width = round(width * scale)
            rgb = F.interpolate(rgb.unsqueeze(0), size=(new_height,
                                                         new_width),
                                 mode='bicubic',
                                 antialias=True).squeeze(0)
            # Pad to a square with white.
            vertical_padding = (input_size - new_height) // 2
            horizontal_padding = (input_size - new_width) // 2
            rgb = F.pad(rgb, (horizontal_padding,
                             input_size - new_width - horizontal_padding,
                             vertical_padding,
                             input_size - new_height - vertical_padding),
                        value=1.0)
            rgb = (rgb - 0.5) / 0.5
            frames.append(rgb)
        return torch.stack(frames)

    def format_caption(self, tags: tuple, probabilities: tuple
                        ) -> tuple[str, str]:
        caption = self.thread.tag_separator.join(tags)
        if self.show_probabilities:
            console_output_caption = self.thread.tag_separator.join(
                f'{tag} ({probability:.2f})'
                for tag, probability in zip(tags, probabilities)
            )
        else:
            console_output_caption = caption
        return caption, console_output_caption

    def get_probabilities_for_inputs(self,
                                     model_inputs: np.ndarray) -> np.ndarray:
        """Preprocess raw RGBA frames on the device and run inference on
        them. `model_inputs` has shape (H, W, 4) for a single frame or
        (N, H, W, 4) for multiple frames; the return value has shape
        (N, tag count)."""
        if model_inputs.ndim == 3:
            frame_arrays = model_inputs[np.newaxis]
        else:
            frame_arrays = model_inputs
        pixel_values = self.preprocess_frames(frame_arrays)
        return self.model.get_probabilities_from_tensors(pixel_values)

    def generate_caption(self, model_inputs: np.ndarray,
                         image_prompt: str) -> tuple[str, str]:
        all_probabilities = self.get_probabilities_for_inputs(model_inputs)
        if all_probabilities.shape[0] > 1:
            probabilities = self.model.aggregate_probabilities(
                list(all_probabilities), self.multiframe_aggregation)
        else:
            probabilities = all_probabilities[0]
        tags, tag_probabilities = self.model.probabilities_to_tags(
            probabilities, self.pixai_tagger_settings)
        return self.format_caption(tags, tag_probabilities)

    def generate_captions_batch(
            self, model_inputs_list: list[np.ndarray]
            ) -> list[tuple[str, str]]:
        """Run inference on a batch of images. The frames of all images
        (animated images and videos contribute multiple frames) are
        preprocessed on the device and flattened into the same forward
        pass, then aggregated per image afterwards."""
        frame_counts = []
        pixel_values_list = []
        for model_inputs in model_inputs_list:
            if model_inputs.ndim == 3:
                frame_arrays = model_inputs[np.newaxis]
            else:
                frame_arrays = model_inputs
            frame_counts.append(frame_arrays.shape[0])
            pixel_values_list.append(self.preprocess_frames(frame_arrays))
        all_frames = torch.cat(pixel_values_list, dim=0)
        all_probabilities = self.model.get_probabilities_from_tensors(
            all_frames)
        captions = []
        start = 0
        for frame_count in frame_counts:
            frame_probabilities = all_probabilities[start:start + frame_count]
            start += frame_count
            if frame_count > 1:
                probabilities = self.model.aggregate_probabilities(
                    list(frame_probabilities), self.multiframe_aggregation)
            else:
                probabilities = frame_probabilities[0]
            tags, tag_probabilities = self.model.probabilities_to_tags(
                probabilities, self.pixai_tagger_settings)
            captions.append(self.format_caption(tags, tag_probabilities))
        return captions
