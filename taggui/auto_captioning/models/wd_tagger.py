# Based on
# https://huggingface.co/spaces/SmilingWolf/wd-tagger/blob/main/app.py.
import csv
import re
from datetime import datetime
from pathlib import Path

import huggingface_hub
import numpy as np
import torch
from PIL import Image as PilImage
from onnxruntime import InferenceSession

import auto_captioning.captioning_thread as captioning_thread
from auto_captioning.auto_captioning_model import AutoCaptioningModel
from utils.enums import CaptionDevice
from utils.image import Image

KAOMOJIS = ['0_0', '(o)_(o)', '+_+', '+_-', '._.', '<o>_<o>', '<|>_<|>', '=_=',
            '>_<', '3_3', '6_9', '>_o', '@_@', '^_^', 'o_o', 'u_u', 'x_x',
            '|_|', '||_||']


def get_tags_to_exclude(tags_to_exclude_string: str) -> list[str]:
    if not tags_to_exclude_string.strip():
        return []
    tags = re.split(r'(?<!\\),', tags_to_exclude_string)
    tags = [tag.strip().replace(r'\,', ',') for tag in tags]
    return tags


class WdTaggerModel:
    def __init__(self, model_id: str, device_setting: CaptionDevice):
        model_path = Path(model_id) / 'model.onnx'
        if not model_path.is_file():
            model_path = huggingface_hub.hf_hub_download(model_id,
                                                         filename='model.onnx')
        tags_path = Path(model_id) / 'selected_tags.csv'
        if not tags_path.is_file():
            tags_path = huggingface_hub.hf_hub_download(
                model_id, filename='selected_tags.csv')
        # Determine providers based on device setting
        if device_setting == CaptionDevice.GPU and torch.cuda.is_available():
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        else:
            providers = ['CPUExecutionProvider']
        self.inference_session = InferenceSession(model_path, providers=providers)
        self.tags = []
        self.rating_tags_indices = []
        self.general_tags_indices = []
        self.character_tags_indices = []
        with open(tags_path, 'r') as tags_file:
            reader = csv.DictReader(tags_file)
            for index, line in enumerate(reader):
                tag = line['name']
                if tag not in KAOMOJIS:
                    tag = tag.replace('_', ' ')
                self.tags.append(tag)
                category = line['category']
                if category == '9':
                    self.rating_tags_indices.append(index)
                elif category == '0':
                    self.general_tags_indices.append(index)
                elif category == '4':
                    self.character_tags_indices.append(index)

        # Boolean mask that selects the non-rating tags; computed once so
        # that the per-image tag post-processing is a cheap vectorized
        # operation instead of Python loops over every tag.
        self.non_rating_mask = np.array(
            [index not in self.rating_tags_indices
             for index in range(len(self.tags))], dtype=bool)
        self.non_rating_tags = [tag for tag, keep in
                                zip(self.tags, self.non_rating_mask) if keep]
        self.input_name = None
        self.output_name = None

    def generate_tags(self, image_array: np.ndarray,
                      wd_tagger_settings: dict) -> tuple[tuple, tuple]:
        if self.input_name is None:
            self.input_name = self.inference_session.get_inputs()[0].name
            self.output_name = self.inference_session.get_outputs()[0].name
        probabilities = self.inference_session.run(
            [self.output_name], {self.input_name: image_array})[0][0].astype(
                np.float32)
        # Some exports output logits; convert them to probabilities.
        if probabilities.min() < 0.0 or probabilities.max() > 1.0:
            probabilities = 1.0 / (1.0 + np.exp(-probabilities))
        # Exclude the rating tags and apply the probability threshold in one
        # vectorized pass.
        probabilities = probabilities[self.non_rating_mask]
        tags = self.non_rating_tags
        keep_indices = np.nonzero(
            probabilities >= wd_tagger_settings['min_probability'])[0]
        tags_to_exclude = get_tags_to_exclude(
            wd_tagger_settings['tags_to_exclude'])
        if tags_to_exclude:
            keep_indices = np.array(
                [index for index in keep_indices
                 if tags[index] not in tags_to_exclude], dtype=int)
        # Sort the tags by probability, highest first, and limit the count.
        if keep_indices.size > 1:
            keep_indices = keep_indices[
                np.argsort(-probabilities[keep_indices], kind='stable')]
        keep_indices = keep_indices[:wd_tagger_settings['max_tags']]
        if keep_indices.size == 0:
            return (), ()
        kept_tags = tuple(tags[index] for index in keep_indices)
        kept_probabilities = tuple(probabilities[index]
                                   for index in keep_indices)
        return kept_tags, kept_probabilities


class WdTagger(AutoCaptioningModel):
    image_mode = 'RGBA'

    def __init__(self,
                 captioning_thread_: 'captioning_thread.CaptioningThread',
                 caption_settings: dict):
        super().__init__(captioning_thread_, caption_settings)
        self.wd_tagger_settings = self.caption_settings['wd_tagger_settings']
        self.show_probabilities = self.wd_tagger_settings['show_probabilities']

    def get_error_message(self) -> str | None:
        return None

    def get_processor(self):
        return None

    def get_model(self):
        return WdTaggerModel(self.model_id, self.device_setting)

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

    def get_model_inputs(self, image_prompt: str, image: Image) -> np.ndarray:
        pil_image = self.load_image(image)
        # Add a white background to the image in case it has transparent areas.
        canvas = PilImage.new('RGBA', pil_image.size, (255, 255, 255))
        canvas.alpha_composite(pil_image)
        pil_image = canvas.convert('RGB')
        # Pad the image to make it square.
        max_dimension = max(pil_image.size)
        canvas = PilImage.new('RGB', (max_dimension, max_dimension),
                              (255, 255, 255))
        horizontal_padding = (max_dimension - pil_image.width) // 2
        vertical_padding = (max_dimension - pil_image.height) // 2
        canvas.paste(pil_image, (horizontal_padding, vertical_padding))
        # Resize the image to the model's input dimensions.
        input_shape = self.model.inference_session.get_inputs()[0].shape
        channels_first = input_shape[1] == 3
        input_dimension = input_shape[2] if channels_first else input_shape[1]
        if max_dimension != input_dimension:
            input_dimensions = (input_dimension, input_dimension)
            canvas = canvas.resize(input_dimensions,
                                   resample=PilImage.Resampling.BICUBIC)
        # Convert the image to a numpy array.
        image_array = np.array(canvas, dtype=np.float32)
        # Reverse the order of the color channels.
        image_array = image_array[:, :, ::-1]  # RGB -> BGR (keep this)
        if channels_first:
            # timm-style export: normalization is not built in.
            image_array = image_array / 127.5 - 1.0  # (x/255 - 0.5) / 0.5
            image_array = np.transpose(image_array, (2, 0, 1))  # HWC -> CHW
        image_array = np.ascontiguousarray(image_array)
        # Add a batch dimension.
        image_array = np.expand_dims(image_array, axis=0)
        return image_array

    def generate_caption(self, model_inputs: np.ndarray,
                         image_prompt: str) -> tuple[str, str]:
        tags, probabilities = self.model.generate_tags(model_inputs,
                                                       self.wd_tagger_settings)
        caption = self.thread.tag_separator.join(tags)
        if self.show_probabilities:
            console_output_caption = self.thread.tag_separator.join(
                f'{tag} ({probability:.2f})'
                for tag, probability in zip(tags, probabilities)
            )
        else:
            console_output_caption = caption
        return caption, console_output_caption
