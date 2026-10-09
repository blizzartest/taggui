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


def load_database_entries(database_path: Path) \
        -> list[tuple[str, list[str]]]:
    """Read the (relative image path, tags) entries of one database file.
    Malformed lines are skipped with a warning."""
    entries = []
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
                entries.append((relative_path, tags))
    except OSError as exception:
        print(f'Failed to read tag database {database_path}: {exception}',
          file=sys.stderr)
    return entries


def load_tags_from_database(directory_path: Path,
                            tag_separator: str) -> dict[Path, list[str]]:
    """
    Load the tag databases (tags.jsonl) of a directory tree: the loaded
    directory gets one database, and each subdirectory gets its own, so
    that any subdirectory can also be loaded as its own directory.

    The keys of the returned dict are absolute image paths. Entries whose
    image file no longer exists are skipped.
    """
    tag_index = {}
    database_paths = {get_tag_database_path(directory_path)}
    for path in directory_path.rglob(TAG_DATABASE_FILENAME):
        database_paths.add(path)
    for database_path in database_paths:
        if not database_path.is_file():
            continue
        database_directory = database_path.parent
        for relative_path, tags in load_database_entries(database_path):
            image_path = Path(os.path.normpath(
                database_directory / relative_path))
            if image_path in tag_index or not image_path.is_file():
                continue
            tag_index[image_path] = [
                str(tag).strip() for tag in tags if str(tag).strip()]
    return tag_index


def write_tags_to_database(directory_path: Path,
                           tag_index: dict[Path, list[str]],
                           prune_missing_images: bool = True):
    """
    Write the tag index to per-directory tag databases (tags.jsonl)
    atomically: every directory that contains tagged images gets its own
    database with image paths relative to that directory, so that any
    subdirectory can also be loaded as its own directory.
    """
    directories: dict[Path, list] = {}
    for image_path, tags in sorted(tag_index.items()):
        if prune_missing_images and not image_path.is_file():
            continue
        image_directory = image_path.parent
        directories.setdefault(image_directory, []).append(
            {'file': image_path.name, 'tags': list(tags)})
    for image_directory, entries in directories.items():
        database_path = get_tag_database_path(image_directory)
        temporary_path = database_path.with_suffix('.jsonl.tmp')
        try:
            with open(temporary_path, 'w', encoding='utf-8') \
                    as database_file:
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


def delete_tag_databases(directory_path: Path) -> int:
    """
    Delete the tag databases (tags.jsonl) of a directory and its
    subdirectories.

    Returns the number of deleted database files.
    """
    database_paths = {get_tag_database_path(directory_path)}
    for path in directory_path.rglob(TAG_DATABASE_FILENAME):
        database_paths.add(path)
    deleted_count = 0
    for database_path in database_paths:
        if not database_path.is_file():
            continue
        try:
            database_path.unlink()
            deleted_count += 1
        except OSError as exception:
            print(f'Failed to delete {database_path}: {exception}',
                  file=sys.stderr)
    return deleted_count


def migrate_txt_tags_to_database(directory_path: Path,
                                 tag_index: dict[Path, list[str]],
                                 delete_txt_files: bool,
                                 tags_subfolder: str = '') \
        -> tuple[int, int]:
    """
    Write the current tag index to the tag database, optionally deleting the
    source .txt files (both next to the images and in the tags subfolder).

    Returns: (migrated_count, deleted_txt_count)
    """
    write_tags_to_database(directory_path, tag_index,
                           prune_missing_images=False)
    migrated_count = len(tag_index)

    deleted_txt_count = 0
    if delete_txt_files:
        deleted_paths = set()
        for image_path in tag_index:
            txt_name = image_path.with_suffix('.txt').name
            possible_txt_paths = [image_path.with_suffix('.txt')]
            if tags_subfolder:
                possible_txt_paths.append(
                    image_path.parent / tags_subfolder / txt_name)
            for txt_path in possible_txt_paths:
                if txt_path in deleted_paths or not txt_path.is_file():
                    continue
                try:
                    txt_path.unlink()
                    deleted_paths.add(txt_path)
                    deleted_txt_count += 1
                except OSError as exception:
                    print(f'Failed to delete {txt_path}: {exception}',
                          file=sys.stderr)

    return migrated_count, deleted_txt_count
