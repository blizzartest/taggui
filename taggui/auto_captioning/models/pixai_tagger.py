from datetime import datetime

import numpy as np
import torch
from PIL import Image as PilImage
from transformers import AutoModel

import auto_captioning.captioning_thread as captioning_thread
from auto_captioning.auto_captioning_model import AutoCaptioningModel
from auto_captioning.models.wd_tagger import (KAOMOJIS,
                                              get_tags_to_exclude)
from utils.enums import CaptionDevice


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

    def generate_tags(self, image_array: np.ndarray,
                      pixai_tagger_settings: dict) -> tuple[tuple, tuple]:
        pixel_values = torch.from_numpy(image_array).unsqueeze(0).to(
            self.device, dtype=next(self.model.parameters()).dtype)
        with torch.inference_mode():
            probabilities = self.model(pixel_values).sigmoid()[0]
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
        # Sort the tags by probability.
        tags_and_probabilities.sort(key=lambda x: x[1], reverse=True)
        tags_and_probabilities = tags_and_probabilities[
            :pixai_tagger_settings['max_tags']]
        if tags_and_probabilities:
            tags, probabilities = zip(*tags_and_probabilities)
        else:
            tags, probabilities = (), ()
        return tags, probabilities


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

    def get_error_message(self) -> str | None:
        return None

    def get_processor(self):
        return None

    def get_model(self):
        return PixaiTaggerModel(self.model_id, self.device, self.dtype,
                                self.pixai_tagger_settings)

    def get_captioning_message(self, are_multiple_images_selected: bool,
                               captioning_start_datetime: datetime) -> str:
        device_str = ('GPU' if (self.device_setting == CaptionDevice.GPU
                       and torch.cuda.is_available()) else 'CPU')
        if are_multiple_images_selected:
            captioning_start_datetime_string = (
                self.get_captioning_start_datetime_string(
                    captioning_start_datetime))
            return (f'Generating tags... (device: {device_str}, start time: '
                    f'{captioning_start_datetime_string})')
        return f'Generating tags... (device: {device_str})'

    def get_model_inputs(self, image_prompt: str,
                         image: 'captioning_thread.Image') -> np.ndarray:
        pil_image = self.load_image(image)
        # Add a white background to the image in case it has transparent
        # areas.
        canvas = PilImage.new('RGBA', pil_image.size, (255, 255, 255))
        canvas.alpha_composite(pil_image)
        pil_image = canvas.convert('RGB')
        input_size = self.model.model.config.img_size
        # Resize the image while preserving its aspect ratio, then pad it to
        # a square.
        width, height = pil_image.size
        scale = input_size / max(width, height)
        new_width = round(width * scale)
        new_height = round(height * scale)
        pil_image = pil_image.resize((new_width, new_height),
                                     resample=PilImage.Resampling.BICUBIC)
        canvas = PilImage.new('RGB', (input_size, input_size),
                              (255, 255, 255))
        horizontal_padding = (input_size - new_width) // 2
        vertical_padding = (input_size - new_height) // 2
        canvas.paste(pil_image, (horizontal_padding, vertical_padding))
        # Normalize the image array to the range expected by the model.
        image_array = np.array(canvas, dtype=np.float32) / 255.0
        image_array = (image_array - 0.5) / 0.5
        # HWC -> CHW.
        image_array = np.transpose(image_array, (2, 0, 1))
        image_array = np.ascontiguousarray(image_array)
        return image_array

    def generate_caption(self, model_inputs: np.ndarray,
                         image_prompt: str) -> tuple[str, str]:
        tags, probabilities = self.model.generate_tags(
            model_inputs, self.pixai_tagger_settings)
        caption = self.thread.tag_separator.join(tags)
        if self.show_probabilities:
            console_output_caption = self.thread.tag_separator.join(
                f'{tag} ({probability:.2f})'
                for tag, probability in zip(tags, probabilities)
            )
        else:
            console_output_caption = caption
        return caption, console_output_caption
