"""
Utilities for reading and writing image metadata (EXIF, IPTC, XMP).

This module provides functions to embed tags into image metadata so they can
be searched in file explorers and software like XnView.

It is designed to be completely standalone and not affect any existing
TagGUI functionality.
"""

import subprocess
import sys
from pathlib import Path


def check_exiftool_available() -> bool:
    """Check if exiftool is available on the system."""
    try:
        result = subprocess.run(
            ['exiftool', '-ver'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def write_tags_to_metadata_exiftool(
    image_path: Path,
    tags: list[str],
    overwrite: bool = False
) -> bool:
    """
    Write tags to image metadata using exiftool.
    
    Writes to both IPTC:Keywords and XMP:Subject for maximum compatibility
    with file explorers and software like XnView.
    
    When overwrite=False (default), appends to existing tags.
    When overwrite=True, replaces existing tags.
    """
    if not tags:
        return True
    
    try:
        tags_str = ', '.join(tags)
        
        if overwrite:
            # Replace existing tags
            cmd = [
                'exiftool',
                '-overwrite_original',
                f'-IPTC:Keywords={tags_str}',
                f'-XMP:Subject={tags_str}',
                str(image_path)
            ]
        else:
            # Append to existing tags
            cmd = [
                'exiftool',
                '-overwrite_original',
                f'-IPTC:Keywords+={tags_str}',
                f'-XMP:Subject+={tags_str}',
                str(image_path)
            ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        return result.returncode == 0
        
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def write_tags_to_metadata_pillow(
    image_path: Path,
    tags: list[str]
) -> bool:
    """
    Write tags to image metadata using Pillow + piexif.
    
    Fallback method that appends tags to EXIF UserComment and ImageDescription.
    Note: This method replaces the fields since piexif doesn't support appending.
    """
    if not tags:
        return True
    
    try:
        from PIL import Image
        import piexif
    except ImportError:
        return False
    
    try:
        tags_str = ', '.join(tags)
        
        with Image.open(image_path) as img:
            exif_dict = {}
            if 'exif' in img.info:
                exif_dict = piexif.load(img.info['exif'])
            else:
                exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}}
            
            # Read existing tags and append new ones
            existing_comment = b''
            existing_description = b''
            
            try:
                existing_comment = exif_dict.get('Exif', {}).get(piexif.ExifIFD.UserComment, b'')
            except (KeyError, AttributeError):
                pass
            
            try:
                existing_description = exif_dict.get('0th', {}).get(piexif.ImageIFD.ImageDescription, b'')
            except (KeyError, AttributeError):
                pass
            
            # Decode existing tags and append new ones
            try:
                existing_tags_str = existing_comment.decode('ascii', errors='ignore').strip()
                if existing_tags_str:
                    tags_str = f'{existing_tags_str}, {tags_str}'
            except Exception:
                pass
            
            try:
                existing_desc_str = existing_description.decode('utf-8', errors='ignore').strip()
                if existing_desc_str:
                    tags_str = f'{existing_desc_str}, {tags_str}'
            except Exception:
                pass
            
            try:
                exif_dict['Exif'][piexif.ExifIFD.UserComment] = tags_str.encode('ascii', errors='replace')
            except (KeyError, AttributeError):
                exif_dict['Exif'] = {piexif.ExifIFD.UserComment: tags_str.encode('ascii', errors='replace')}
            
            try:
                exif_dict['0th'][piexif.ImageIFD.ImageDescription] = tags_str.encode('utf-8', errors='replace')
            except (KeyError, AttributeError):
                exif_dict['0th'] = {piexif.ImageIFD.ImageDescription: tags_str.encode('utf-8', errors='replace')}
            
            exif_bytes = piexif.dump(exif_dict)
            img.save(image_path, exif=exif_bytes)
        
        return True
        
    except Exception:
        return False


def write_tags_to_metadata(
    image_path: Path,
    tags: list[str],
    use_exiftool: bool = True,
    overwrite: bool = False
) -> bool:
    """
    Write tags to image metadata using the best available method.
    
    Args:
        image_path: Path to the image file
        tags: List of tags to write
        use_exiftool: Whether to try exiftool first
        overwrite: If True, replace existing tags. If False (default), append to existing tags.
    """
    if not tags:
        return True
    
    if use_exiftool and check_exiftool_available():
        if write_tags_to_metadata_exiftool(image_path, tags, overwrite=overwrite):
            return True
    
    return write_tags_to_metadata_pillow(image_path, tags)


def export_tags_to_metadata_for_directory(
    directory_path: Path,
    use_exiftool: bool = True,
    overwrite: bool = False
) -> tuple[int, int]:
    """
    Export tags from .txt files to image metadata for all images in a directory.
    
    Args:
        directory_path: Path to directory with images and .txt files
        use_exiftool: Whether to use exiftool (falls back to Pillow)
        overwrite: If True, replace existing tags. If False (default), append to existing tags.
    
    Returns: (successful_count, failed_count)
    """
    image_suffixes = ['.bmp', '.gif', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp']
    
    image_paths = [
        path for path in directory_path.iterdir()
        if path.is_file() and path.suffix.lower() in image_suffixes
    ]
    
    successful = 0
    failed = 0
    
    for image_path in image_paths:
        txt_path = image_path.with_suffix('.txt')
        if not txt_path.exists():
            continue
        
        try:
            caption = txt_path.read_text(encoding='utf-8', errors='replace')
            tags = [tag.strip() for tag in caption.split(',') if tag.strip()]
        except Exception:
            failed += 1
            continue
        
        if write_tags_to_metadata(image_path, tags, use_exiftool, overwrite=overwrite):
            successful += 1
        else:
            failed += 1
    
    return successful, failed
