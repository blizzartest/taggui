import json
import os
import sys
from pathlib import Path

from utils.settings import DEFAULT_SETTINGS, get_settings

TAG_DATABASE_FILENAME = 'tags.jsonl'


def get_tags_storage_mode() -> str:
    """Return the current tags storage mode ('single_file' or 'txt_files')."""
    settings = get_settings()
    return settings.value(
        'tags_storage_mode',
        defaultValue=DEFAULT_SETTINGS['tags_storage_mode'], type=str)


def get_tag_database_path(directory_path: Path) -> Path:
    return directory_path / TAG_DATABASE_FILENAME


def load_tags_from_database(directory_path: Path,
                            tag_separator: str) -> dict[Path, list[str]]:
    """
    Load the tag database (tags.jsonl) for a directory tree.

    The keys of the returned dict are absolute image paths. Entries whose
    image file no longer exists are skipped.
    """
    tag_index = {}
    database_path = get_tag_database_path(directory_path)
    if not database_path.is_file():
        return tag_index
    try:
        with open(database_path, encoding='utf-8') as database_file:
            for line_number, line in enumerate(database_file, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exception:
                    print(f'Failed to parse {database_path} line '
                          f'{line_number}: {exception}', file=sys.stderr)
                    continue
                relative_path = entry.get('file')
                tags = entry.get('tags')
                if not isinstance(relative_path, str) or not isinstance(
                        tags, list):
                    continue
                image_path = Path(os.path.normpath(
                    directory_path / relative_path))
                if not image_path.is_file():
                    continue
                tag_index[image_path] = [
                    str(tag).strip() for tag in tags if str(tag).strip()]
    except OSError as exception:
        print(f'Failed to read tag database {database_path}: {exception}',
              file=sys.stderr)
    return tag_index


def write_tags_to_database(directory_path: Path,
                           tag_index: dict[Path, list[str]],
                           prune_missing_images: bool = True):
    """
    Write the tag index to the tag database (tags.jsonl) atomically.

    Image paths are stored relative to the directory so that the database
    stays portable when the directory is moved or shared.
    """
    database_path = get_tag_database_path(directory_path)
    entries = []
    for image_path, tags in sorted(tag_index.items()):
        if prune_missing_images and not image_path.is_file():
            continue
        try:
            relative_path = image_path.relative_to(directory_path)
        except ValueError:
            relative_path = image_path
        entries.append({'file': relative_path.as_posix(),
                        'tags': list(tags)})
    temporary_path = database_path.with_suffix('.jsonl.tmp')
    try:
        with open(temporary_path, 'w', encoding='utf-8') as database_file:
            for entry in entries:
                database_file.write(
                    json.dumps(entry, ensure_ascii=False) + '\n')
        os.replace(temporary_path, database_path)
    except OSError as exception:
        print(f'Failed to write tag database {database_path}: {exception}',
              file=sys.stderr)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def migrate_txt_tags_to_database(directory_path: Path,
                                 tag_index: dict[Path, list[str]],
                                 delete_txt_files: bool) -> tuple[int, int]:
    """
    Write the current tag index to the tag database, optionally deleting the
    source .txt files.

    Returns: (migrated_count, deleted_txt_count)
    """
    write_tags_to_database(directory_path, tag_index,
                           prune_missing_images=False)
    migrated_count = len(tag_index)

    deleted_txt_count = 0
    if delete_txt_files:
        for image_path in tag_index:
            txt_path = image_path.with_suffix('.txt')
            if txt_path.is_file():
                try:
                    txt_path.unlink()
                    deleted_txt_count += 1
                except OSError as exception:
                    print(f'Failed to delete {txt_path}: {exception}',
                          file=sys.stderr)

    return migrated_count, deleted_txt_count
