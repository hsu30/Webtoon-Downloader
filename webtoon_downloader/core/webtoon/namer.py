import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from furl import furl

from webtoon_downloader.core.webtoon.models import ChapterInfo, PageInfo

ChapterNamingMode: TypeAlias = Literal["title", "number", "number-title"]
"""Valid modes for naming separate chapter directories."""

_WINDOWS_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class InvalidChapterNamingModeError(ValueError):
    """Raised when a chapter directory naming mode is unsupported."""

    def __init__(self, mode: object) -> None:
        super().__init__(f"Unsupported chapter naming mode: {mode}")


def sanitize_filename(filename: str) -> str:
    """Return a safe filename while preserving readable spaces and parentheses."""
    # A forward slash creates nested paths on every supported platform.
    filename = filename.replace("/", "_").replace("\x00", "_")

    if os.name == "nt":
        filename = _WINDOWS_INVALID_FILENAME_CHARS.sub("_", filename).rstrip(" .")
        stem = filename.split(".", maxsplit=1)[0].upper()
        if stem in _WINDOWS_RESERVED_FILENAMES:
            filename = f"_{filename}"

    if filename in {"", ".", ".."}:
        return "_"

    return filename


@runtime_checkable
class FileNameGenerator(Protocol):
    """
    A protocol defining the interface for generating file names for chapters, pages and exporters.
    """

    def get_chapter_directory(self, chapter_info: ChapterInfo) -> Path:
        """
        Returns the directory path for storing the given chapter's data.
        """

    def get_page_filename(self, page_info: PageInfo) -> str:
        """
        Generates a file name for the given page.
        """

    def get_title_filename(self, chapter_info: ChapterInfo) -> str:
        """
        Generates a file name for the title exporter of the given chapter.
        """

    def get_notes_filename(self, chapter_info: ChapterInfo) -> str:
        """
        Generates a file name for the notes exporter of the given chapter.
        """


@dataclass
class SeparateFileNameGenerator(FileNameGenerator):
    """
    Name Generator for when chapters and pages are stored separately.
    """

    use_chapter_title_directories: bool = False
    chapter_mode: ChapterNamingMode | None = None

    def __post_init__(self) -> None:
        if self.chapter_mode not in {None, "title", "number", "number-title"}:
            raise InvalidChapterNamingModeError(self.chapter_mode)

    def get_chapter_directory(self, chapter_info: ChapterInfo) -> Path:
        """
        Returns the directory path for storing the given chapter's data.
        """
        chapter_mode = self.chapter_mode
        if chapter_mode is None:
            chapter_mode = "title" if self.use_chapter_title_directories else "number"

        chapter_number = f"{chapter_info.number:0{len(str(chapter_info.total_chapters))}d}"
        if chapter_mode == "title":
            return Path(sanitize_filename(chapter_info.title))
        if chapter_mode == "number":
            return Path(chapter_number)

        return Path(f"{chapter_number}-{sanitize_filename(chapter_info.title)}")

    def get_page_filename(self, page_info: PageInfo) -> str:
        """
        Generates a file name for a page, using the page number and the file extension from its URL.
        """
        page_number = f"{page_info.page_number:0{len(str(page_info.total_pages))}d}"
        extension = furl(page_info.url).path.segments[-1].split(".")[-1]
        return f"{page_number}.{extension}"

    def get_title_filename(self, chapter_info: ChapterInfo) -> str:
        """
        Generates a file name for the title exporter of the given chapter.
        """
        return "title.txt"

    def get_notes_filename(self, chapter_info: ChapterInfo) -> str:
        """
        Generates a file name for the notes exporter of the given chapter.
        """
        return "notes.txt"


class NonSeparateFileNameGenerator(FileNameGenerator):
    """
    Implementation of FileNameGenerator for generating file names when chapters and pages
    are stored in the same directory.
    """

    def get_chapter_directory(self, chapter_info: ChapterInfo) -> Path:
        """
        Returns the root directory for storing pages when they are not separated by chapters.
        """
        return Path(".")

    def get_page_filename(self, page_info: PageInfo) -> str:
        """
        Generates a file name for a page, combining the chapter number and page number,
        along with the file extension from its URL.
        """
        chapter_number = f"{page_info.chapter_info.number:0{len(str(page_info.chapter_info.total_chapters))}d}"
        page_number = f"{page_info.page_number:0{len(str(page_info.total_pages))}d}"
        extension = furl(page_info.url).path.segments[-1].split(".")[-1]
        return f"{chapter_number}_{page_number}.{extension}"

    def get_title_filename(self, chapter_info: ChapterInfo) -> str:
        """
        Generates a file name for the title exporter of the given chapter.
        """
        return f"{chapter_info.number:0{len(str(chapter_info.total_chapters))}d}_title.txt"

    def get_notes_filename(self, chapter_info: ChapterInfo) -> str:
        """
        Generates a file name for the notes exporter of the given chapter.
        """
        return f"{chapter_info.number:0{len(str(chapter_info.total_chapters))}d}_notes.txt"
