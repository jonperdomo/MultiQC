"""
MultiQC report module. Holds the output from each module. Available to subsequent
modules. Contains helper functions to generate markup for report.
"""

import base64
import dataclasses
import fnmatch
import gzip
import inspect
import io
import json
import logging
import mimetypes
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PosixPath
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    TextIO,
    Tuple,
    Union,
)

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from multiqc import config

# This does not cause circular imports because BaseMultiqcModule is used only in
# quoted type hints, and quoted type hints are lazily evaluated:
from multiqc.base_module import BaseMultiqcModule
from multiqc.core import ai, log_and_rich, tmp_dir
from multiqc.core.exceptions import NoAnalysisFound
from multiqc.core.log_and_rich import iterate_using_progress_bar
from multiqc.core.tmp_dir import data_tmp_dir
from multiqc.plots.plotly.plot import Plot
from multiqc.plots.plotly.violin import ViolinPlot
from multiqc.plots.table_object import ColumnDict, InputRow, SampleName, ValueT
from multiqc.types import Anchor, ColumnKey, FileDict, ModuleId, SampleGroup, Section
from multiqc.utils import megaqc
from multiqc.utils.util_functions import (
    dump_json,
    replace_defaultdicts,
    rmtree_with_retries,
)

load_dotenv()

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Runtimes:
    total: float = 0.0
    total_sp: float = 0.0
    total_mods: float = 0.0
    total_compression: float = 0.0
    sp: Dict[str, float] = dataclasses.field(default_factory=lambda: defaultdict())
    mods: Dict[str, float] = dataclasses.field(default_factory=lambda: defaultdict())


initialized = False
###########
### Below are uninitialised global variables for static typing
# Fields below are reset between interactive runs
multiqc_command: str
creation_date: datetime
top_modules: List[Dict[str, Dict[str, str]]]
module_order: List[Dict[str, Dict[str, Union[str, List[str]]]]]
modules: List["BaseMultiqcModule"]  # list of BaseMultiqcModule objects
general_stats_plot: Optional[ViolinPlot]
general_stats_html: str
lint_errors: List[str]
num_flat_plots: int
some_plots_are_deferred: bool
last_found_file: Optional[str]
runtimes: Runtimes
peak_memory_bytes_per_module: Dict[str, int]
diff_memory_bytes_per_module: Dict[str, int]
report_uuid: str

# File search state - also reset between interactive runs
analysis_files: List[str]  # input files to search
files: Dict[ModuleId, List[FileDict]]  # files found by each module
file_search_stats: Dict[str, Set[Path]]

# AI stuff, set dynamically in ai.py to be used in content.html and ai.js
ai_global_summary: str = ""
ai_global_detailed_analysis: str = ""
ai_thread_id: str = ""
ai_provider_id: str = ""
ai_provider_title: str = ""
ai_model: str = ""
ai_model_resolved: str = ""
ai_report_metadata_base64: str = ""  # to copy/generate AI summaries from the report JS runtime
sample_names: List[SampleName] = []  # all sample names in the report to construct ai_pseudonym_map
ai_pseudonym_map: Dict[str, str] = {}
ai_pseudonym_map_base64: str = ""

# Following fields are preserved between interactive runs
data_sources: Dict[str, Dict[str, Dict[str, Any]]]
html_ids_by_scope: Dict[Optional[str], Set[Anchor]] = defaultdict(set)
plot_data: Dict[Anchor, Dict[str, Any]] = dict()  # plot dumps to embed in html
plot_by_id: Dict[Anchor, Plot[Any, Any]] = dict()  # plot objects for interactive use
general_stats_data: List[Dict[SampleGroup, List[InputRow]]]
general_stats_headers: List[Dict[ColumnKey, ColumnDict]]
software_versions: Dict[str, Dict[str, List[str]]]  # map software tools to unique versions
plot_compressed_json: str
saved_raw_data_keys: List[str]  # to make sure write_data_file don't overwrite for repeated modules
saved_raw_data: Dict[str, Any] = dict()  # only populated if preserve_module_raw_data is enabled


def reset():
    # Set up global variables shared across modules. Inside a function so that the global
    # vars are reset if MultiQC is run more than once within a single session / environment.
    global initialized

    global multiqc_command
    global creation_date
    global top_modules
    global module_order
    global analysis_files
    global modules
    global general_stats_plot
    global general_stats_html
    global lint_errors
    global num_flat_plots
    global some_plots_are_deferred
    global last_found_file
    global runtimes
    global peak_memory_bytes_per_module
    global diff_memory_bytes_per_module
    global file_search_stats
    global files
    global report_uuid

    global data_sources
    global html_ids_by_scope
    global plot_data
    global plot_by_id
    global general_stats_data
    global general_stats_headers
    global software_versions
    global plot_compressed_json
    global saved_raw_data_keys
    global saved_raw_data

    global ai_global_summary
    global ai_global_detailed_analysis
    global ai_thread_id
    global ai_provider_id
    global ai_provider_title
    global ai_model
    global ai_model_resolved
    global ai_report_metadata_base64
    global sample_names
    global ai_pseudonym_map

    # Create new temporary directory for module data exports
    initialized = True

    multiqc_command = ""
    creation_date = datetime.now().astimezone()
    top_modules = []
    module_order = []
    modules = []
    general_stats_plot = None
    general_stats_html = ""
    lint_errors = []
    num_flat_plots = 0
    some_plots_are_deferred = False
    last_found_file = None
    runtimes = Runtimes()
    peak_memory_bytes_per_module = dict()
    diff_memory_bytes_per_module = dict()
    report_uuid = ""

    reset_file_search()

    ai_global_summary = ""
    ai_global_detailed_analysis = ""
    ai_thread_id = ""
    ai_provider_id = ""
    ai_provider_title = ""
    ai_model = ""
    ai_model_resolved = ""
    ai_report_metadata_base64 = ""
    sample_names = []
    ai_pseudonym_map = {}
    data_sources = defaultdict(lambda: defaultdict(lambda: defaultdict()))
    html_ids_by_scope = defaultdict(set)
    plot_data = dict()
    plot_by_id = dict()
    general_stats_data = []
    general_stats_headers = []
    software_versions = defaultdict(lambda: defaultdict(list))
    plot_compressed_json = ""
    saved_raw_data_keys = []
    saved_raw_data = dict()

    tmp_dir.new_tmp_dir()


def reset_file_search():
    """
    Reset the file search session. Useful in interactive session to call in the end
    of parse_logs(), so the next run is not affected by the previous one.
    """
    global analysis_files
    global files
    global file_search_stats
    analysis_files = []
    files = dict()  # Discovered files for each search key
    file_search_stats = {
        "skipped_symlinks": set(),
        "skipped_not_a_file": set(),
        "skipped_ignore_pattern": set(),
        "skipped_filesize_limit": set(),
        "skipped_module_specific_max_filesize": set(),
        "skipped_no_match": set(),
        "skipped_directory_fn_ignore_dirs": set(),
        "skipped_file_contents_search_errors": set(),
    }


reset()


def file_line_block_iterator(fp: TextIO, block_size: int = 4096) -> Iterator[Tuple[int, str]]:
    """
    Iterate over fileblocks that only contain complete lines. The last
    character of each block is always '\n' unless it is the last line and no
    terminating newline was present. No truncated lines are present in the
    blocks, allowing for searching per-line-matching contents.
    A tuple with the number of newlines and the block is yielded on each
    iteration.

    The default block_size is 4096, which is equal to the filesystem block
    size.
    """
    remainder = ""
    while True:
        block = fp.read(block_size)
        if block == "":  # EOF
            # The last line may not have a terminating newline character
            if remainder:
                yield 1, remainder
            return
        number_of_newlines = block.count("\n")
        if number_of_newlines == 0:
            # Use readline function so only one call is needed to complete the
            # block.
            block += fp.readline()
            number_of_newlines = 1
            block_end = len(block)
        else:
            block_end = block.rfind("\n") + 1  # + 1 to include the '\n'
        yield number_of_newlines, remainder + block[:block_end]
        # Store the remainder for the next iteration.
        remainder = block[block_end:]


class SearchFile:
    """
    Wrap file handler and provide a lazy line block iterator on it with caching.

    The class is a context manager with context being an open file handler:

    The `self.line_block_iterator()` method is a distinct context manager over
    the file line blocks, that can be called multiple times for the same open file handler
    due to line block caching.

    with SearchFile(fn, root):
        for line_count, block in f.line_block_iterator():
            # process block

        # start again, this time will read from cache:
        for line_count, block in f.line_block_iterator():
            # process block again
    """

    def __init__(self, path: Path):
        self.path: Path = path
        self.filename = path.name
        self.root = path.parent
        self._filehandle: Optional[TextIO] = None
        self._iterator: Optional[Iterator[Tuple[int, str]]] = None
        self._blocks: List[Tuple[int, str]] = []  # cache of read blocks with line count found in each block
        self._filesize: Optional[int] = None

    @property
    def filesize(self) -> Optional[int]:
        if self._filesize is None:
            try:
                self._filesize = os.path.getsize(self.path)
            except (IOError, OSError, ValueError, UnicodeDecodeError):
                logger.debug(f"Couldn't read file when checking filesize: {self.filename}")
                self._filesize = None
        return self._filesize

    def line_block_iterator(self) -> Iterator[Tuple[int, str]]:
        """
        Optimized file line iterator.

        Yields a tuple with the number of newlines and a chunk.

        Can be called multiple times for the same open file handler.

        Serves as a replacement for f.readlines() that is:
        - lazy
        - caches 4kb+ blocks
        - returns multiple lines concatenated with '\n' if found in block

        This way, we can compare whole blocks versus the search patterns, instead of individual lines,
        and each comparison comes with a lot of Python runtime overhead.

        First loops over the cache `self._blocks`, then tries to read more blocks from the file handler.
        """
        for count_and_block_tuple in self._blocks:
            yield count_and_block_tuple
        if self._filehandle is None:
            try:
                self._filehandle = io.open(self.path, "rt", encoding="utf-8")
            except Exception as e:
                if config.report_readerrors:
                    logger.debug(f"Couldn't read file when looking for output: {self.path}, {e}")
                raise
            self._iterator = file_line_block_iterator(self._filehandle)
        assert self._iterator is not None
        try:
            for count_and_block_tuple in self._iterator:
                self._blocks.append(count_and_block_tuple)
                yield count_and_block_tuple

        except UnicodeDecodeError as e:
            if config.report_readerrors:
                logger.debug(
                    f"Couldn't read file as a utf-8 text when looking for output: {self.path}, {e}. "
                    f"Usually because it's a binary file. But sometimes there are single non-unicode "
                    f"characters, so attempting reading while skipping such characters."
                )
        except Exception as e:
            if config.report_readerrors:
                logger.debug(f"Couldn't read file when looking for output: {self.path}, {e}")
            raise
        else:
            # When no lines are parsed, self.content_lines should be empty
            if not self._blocks and config.report_readerrors:
                logger.debug(f"No utf-8 lines were read from the file, skipping {self.path}")
            return  # No errors
        self._filehandle.close()
        self._filehandle = io.open(self.path, "rt", encoding="utf-8", errors="ignore")
        self._iterator = file_line_block_iterator(self._filehandle)
        try:
            if self._blocks:
                # Skip all the lines that were already read
                for _ in self._blocks:
                    next(self._iterator)
            for count_and_block_tuple in self._iterator:
                self._blocks.append(count_and_block_tuple)
                yield count_and_block_tuple
        except Exception as e:
            if config.report_readerrors:
                logger.debug(f"Still couldn't read the file, skipping: {self.path}, {e}")

        if not self._blocks and config.report_readerrors:
            logger.debug(f"No utf-8 lines were read from the file, skipping {self.path}")

    def line_iterator(self) -> Iterator[Tuple[int, str]]:
        total_line_count = 0
        for _line_count, line_block in self.line_block_iterator():
            for line in line_block.split("\n"):
                total_line_count += 1
                yield total_line_count, line

    def close(self):
        if self._filehandle:
            self._filehandle.close()
        self._iterator = None
        self._filehandle = None

    def closed(self):
        return bool(self._filehandle)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def to_dict(self, sp_key: str) -> FileDict:
        return {"fn": self.filename, "root": str(self.root), "sp_key": sp_key}


def is_searching_in_source_dir(path: Path) -> bool:
    """
    Checks whether MultiQC is searching for files in the source code folder
    """
    multiqc_installation_dir_files = [
        "LICENSE",
        "CHANGELOG.md",
        "Dockerfile",
        "MANIFEST.in",
        ".gitmodules",
        "README.md",
        "pyproject.toml",
        ".gitignore",
    ]

    filenames = [f.name for f in path.iterdir() if f.is_file()]

    if len(filenames) > 0 and all([fn in filenames for fn in multiqc_installation_dir_files]):
        logger.error(f"Error: MultiQC is running in source code directory! {path}")
        logger.warning("Please see the docs for how to use MultiQC: https://docs.seqera.io/multiqc/#running-multiqc")
        return True
    else:
        return False


class SearchPattern(BaseModel):
    fn: Optional[str] = None
    fn_re: Optional[re.Pattern] = None
    contents: Set[str] = Field(default_factory=set)
    contents_re: Set[re.Pattern] = Field(default_factory=set)
    num_lines: Optional[int] = None
    shared: bool = False
    skip: bool = False
    max_filesize: Optional[int] = None
    exclude_fn: Set[str] = Field(default_factory=set)
    exclude_fn_re: Set[re.Pattern] = Field(default_factory=set)
    exclude_contents: Set[str] = Field(default_factory=set)
    exclude_contents_re: Set[re.Pattern] = Field(default_factory=set)

    @staticmethod
    def parse(d: Dict, key: str) -> Optional["SearchPattern"]:
        one_of_required = ["fn", "fn_re", "contents", "contents_re"]
        if not any(d.get(k) for k in one_of_required):
            msg = f"At least one of the keys must be specified in search pattern: {one_of_required}, got: '{key}': {d}"
            logger.error(msg)
            if config.strict:
                lint_errors.append(msg)
            return None

        # Convert the values that can be lists/sets or str into sets
        for k in ["contents", "contents_re", "exclude_fn", "exclude_fn_re", "exclude_contents", "exclude_contents_re"]:
            val = d.get(k, [])
            if val:
                strs = [val] if isinstance(val, str) else val
                d[k] = set(strs)

        # Convert patterns into regex objects
        for k in ["contents_re", "exclude_fn_re", "exclude_contents_re"]:
            strs = d.get(k, [])
            if strs:
                d[k] = {re.compile(s) for s in set(strs)}

        if "fn_re" in d:
            d["fn_re"] = re.compile(d["fn_re"])

        return SearchPattern(**d)


def prep_ordered_search_files_list(
    sp_keys: List[ModuleId],
) -> Tuple[List[Dict[ModuleId, List[SearchPattern]]], List[Path]]:
    """
    Prepare the searchfiles list in desired order, from easy to difficult;
    apply ignore_dirs and ignore_paths filters.
    """

    spatterns: List[Dict[ModuleId, List[SearchPattern]]] = [{}, {}, {}, {}, {}, {}, {}]
    searchfiles: List[Path] = []

    def _maybe_add_path_to_searchfiles(item: Path):
        """
        Branching logic to handle analysis paths (directories and files)
        Walks directory trees recursively calling pathlib's `path.iterdir()`.
        Guaranteed to work correctly with symlinks even on non-POSIX compliant filesystems.
        """
        if item.is_symlink() and config.ignore_symlinks:
            file_search_stats["skipped_symlinks"].add(item)
            return
        elif item.is_file():
            searchfiles.append(item)
        elif item.is_dir():
            # Skip directory if it matches ignore patterns
            d_matches = any(d for d in config.fn_ignore_dirs if item.match(d.rstrip(os.sep)))
            p_matches = any(p for p in config.fn_ignore_paths if item.match(p.rstrip(os.sep)))
            if d_matches or p_matches:
                file_search_stats["skipped_directory_fn_ignore_dirs"].add(item)
                return

            # Check not running in install directory
            if is_searching_in_source_dir(item):
                return

            for item in item.iterdir():
                _maybe_add_path_to_searchfiles(item)

    ignored_patterns = []
    skipped_patterns = []

    for key, sp_dicts in config.sp.items():
        mod_key = key.split("/", 1)[0]
        if mod_key.lower() not in [m.lower() for m in sp_keys]:
            ignored_patterns.append(key)
            continue
        if not isinstance(sp_dicts, list):
            sp_dicts = [sp_dicts]

        # Warn if we have any unrecognised search pattern keys
        expected_sp_keys = [k for k in SearchPattern.model_fields]
        unrecognised_keys = [y for x in sp_dicts for y in x.keys() if y not in expected_sp_keys]
        if len(unrecognised_keys) > 0:
            logger.warning(f"Unrecognised search pattern keys for '{key}': {', '.join(unrecognised_keys)}")

        sps: List[SearchPattern] = [v for v in [SearchPattern.parse(d, key) for d in sp_dicts] if v is not None]

        # Check if we are skipping this search key
        if any([x.skip for x in sps]):
            skipped_patterns.append(key)

        # Split search patterns according to speed of execution.
        if any([x for x in sps if x.contents_re]):
            if any([x for x in sps if x.num_lines is not None]):
                spatterns[4][key] = sps
            elif any([x for x in sps if x.max_filesize is not None]):
                spatterns[5][key] = sps
            else:
                spatterns[6][key] = sps
        elif any([x for x in sps if x.contents]):
            if any([x for x in sps if x.num_lines is not None]):
                spatterns[1][key] = sps
            elif any([x for x in sps if x.max_filesize is not None]):
                spatterns[2][key] = sps
            else:
                spatterns[3][key] = sps
        else:
            spatterns[0][key] = sps

    def _sort_by_key(_sps: Dict[ModuleId, List[SearchPattern]], _key: str) -> Dict[ModuleId, List[SearchPattern]]:
        """Sort search patterns dict by key like num_lines or max_filesize."""

        def _sort_key(kv) -> int:
            _, __sps = kv
            # Since modules can have multiple search patterns, we sort by the slowest one:
            return max([getattr(sp, _key) or 0 for sp in __sps])

        return dict(sorted(_sps.items(), key=_sort_key))

    # Sort patterns for faster access. File searches with fewer lines or
    # smaller file sizes go first.
    sorted_spatterns: List[Dict[ModuleId, List[SearchPattern]]] = [
        {},
        {},
        {},
        {},
        {},
        {},
        {},
    ]
    sorted_spatterns[0] = spatterns[0]  # Only filename matching
    sorted_spatterns[1] = _sort_by_key(spatterns[1], "num_lines")
    sorted_spatterns[2] = _sort_by_key(spatterns[2], "max_filesize")
    sorted_spatterns[3] = spatterns[3]
    sorted_spatterns[4] = _sort_by_key(spatterns[4], "num_lines")
    sorted_spatterns[5] = _sort_by_key(spatterns[5], "max_filesize")
    sorted_spatterns[6] = spatterns[6]
    spatterns = sorted_spatterns

    if len(ignored_patterns) > 0:
        logger.debug(
            f"Ignored {len(ignored_patterns)} search patterns that didn't match running modules"
            + (f": {ignored_patterns}" if len(ignored_patterns) < 10 else "")
        )

    if len(skipped_patterns) > 0:
        logger.info(f"Skipping {len(skipped_patterns)} file search patterns")
        logger.debug(f"Skipping search patterns: {', '.join(skipped_patterns)}")

    # Go through the analysis directories and get file list in searchfiles
    for path in analysis_files:
        _maybe_add_path_to_searchfiles(Path(path))

    return spatterns, searchfiles


def run_search_files(spatterns: List[Dict[ModuleId, List[SearchPattern]]], searchfiles: List[Path]):
    runtimes.sp = defaultdict()
    total_sp_starttime = time.time()

    def add_file(path: Path):
        """
        Function applied to each file found when walking the analysis
        directories. Runs through all search patterns and returns True
        if a match is found.
        """
        search_f = SearchFile(path)

        # Check that this is a file and not a pipe or anything weird
        if not path.is_file():
            file_search_stats["skipped_not_a_file"].add(path)
            return False

        if search_f.filesize is not None and search_f.filesize > config.log_filesize_limit:
            file_search_stats["skipped_filesize_limit"].add(path)
            return False

        # Use mimetypes to exclude binary files where possible
        if not re.match(r".+_mqc\.(png|jpg|jpeg|gif|webp|tiff)", search_f.filename) and config.ignore_images:
            (ftype, encoding) = mimetypes.guess_type(str(path))
            if encoding is not None and encoding != "gzip":
                return False
            if ftype is not None and ftype.startswith("image"):
                return False

        # Check if file is in ignore files
        is_ignore_file = False
        for ignore_pat in config.fn_ignore_files:
            if fnmatch.fnmatch(search_f.filename, ignore_pat):
                is_ignore_file = True

        # Test file for each search pattern
        file_matched = False
        with search_f:  # Ensure any open filehandles are closed.
            for patterns in spatterns:
                for module_id, sps in patterns.items():
                    start = time.time()
                    for sp in sps:
                        if search_file(sp, search_f, module_id, is_ignore_file):
                            # Check that we shouldn't exclude this file
                            if not exclude_file(sp, search_f):
                                # Looks good! Remember this file
                                if module_id not in files:
                                    files[module_id] = []
                                files[module_id].append(search_f.to_dict(sp_key=module_id))
                                file_search_stats[module_id] = file_search_stats.get(module_id, set()) | {path}
                                file_matched = True
                                # logger.debug(f"File {f.path} matched {module_id}")
                            # Don't keep searching this file for other modules
                            if not sp.shared and module_id not in config.filesearch_file_shared:
                                runtimes.sp[module_id] = runtimes.sp.get(module_id, 0) + (time.time() - start)
                                return True
                            # Don't look at other patterns for this module
                            break
                    runtimes.sp[module_id] = runtimes.sp.get(module_id, 0) + (time.time() - start)
        return file_matched

    def update_fn(_, sf: Path):
        if not add_file(sf):
            file_search_stats["skipped_no_match"].add(sf)

    iterate_using_progress_bar(
        items=searchfiles,
        update_fn=update_fn,
        desc="searching",
    )

    runtimes.total_sp = time.time() - total_sp_starttime
    if config.profile_runtime:
        logger.info(f"Profile-runtime: Searching files took {runtimes.total_sp:.2f}s")

    # Debug log summary about what we skipped
    summaries: List[str] = []
    for key in sorted(file_search_stats, key=lambda x: file_search_stats[x], reverse=True):
        if "skipped_" in key and file_search_stats[key]:
            summaries.append(f"{key}: {len(file_search_stats[key])}")
    if summaries:
        logger.debug(f"Summary of files that were skipped by the search: |{'|, |'.join(summaries)}|")


def search_files(sp_keys: List[str]):
    """
    Go through all supplied search directories and assembly a master
    list of files to search. Then fire search functions for each file.
    """
    if not analysis_files:
        raise NoAnalysisFound("No analysis files found to search")

    spatterns, searchfiles = prep_ordered_search_files_list([ModuleId(k) for k in sp_keys])

    run_search_files(spatterns, searchfiles)


def search_file(
    pattern: SearchPattern,
    f: SearchFile,
    module_key: ModuleId,
    is_ignore_file: bool = False,
):
    """
    Function to search a single file for a single search pattern.
    """

    global file_search_stats

    # Search pattern specific filesize limit
    if pattern.max_filesize is not None and f.filesize:
        if f.filesize > pattern.max_filesize:
            file_search_stats["skipped_module_specific_max_filesize"].add(f.path)
            return False

    # Search by file name (glob)
    if pattern.fn is not None:
        if not fnmatch.fnmatch(f.filename, pattern.fn):
            return False

    # Search by file name (regex)
    if pattern.fn_re is not None:
        if not re.match(pattern.fn_re, f.filename):
            return False

    # If we only had fn and fn_re, can assume matching is done:
    if not pattern.contents and not pattern.contents_re:
        return True

    if is_ignore_file:
        # Ignore filenames are never searched for content.
        file_search_stats["skipped_ignore_pattern"].add(f.path)
        return False

    # Search by file contents
    num_lines = pattern.num_lines or config.filesearch_lines_limit

    total_lines = 0
    query_strings = pattern.contents
    query_re_patterns = pattern.contents_re
    match_strings: Set[str] = set()
    match_re_patterns: Set[re.Pattern] = set()

    try:
        for line_count, block in f.line_block_iterator():
            for q_string in query_strings:
                if q_string in block:
                    if total_lines + line_count > num_lines:
                        # We read more lines than requested and the match may
                        # be in a part of the file that shouldn't be read.
                        # Test how many lines preceed the match to see if there
                        # was overshoot.
                        s_index = block.index(q_string)
                        lines_including_match = block[:s_index].count("\n") + 1
                        if total_lines + lines_including_match <= num_lines:
                            match_strings.add(q_string)
                    else:
                        match_strings.add(q_string)
                    if len(match_strings) == len(query_strings):  # all strings matched
                        break
            for q_pattern in query_re_patterns:
                # Limit the number of lines to the amount of lines that should remain
                for line in block.splitlines(keepends=True)[: num_lines - total_lines]:
                    if q_pattern.match(line):
                        match_re_patterns.add(q_pattern)
                        if len(match_re_patterns) == len(query_re_patterns):  # all strings matched
                            break
            total_lines += line_count
            if total_lines >= num_lines:
                break
    except Exception:
        file_search_stats["skipped_file_contents_search_errors"].add(f.path)
        return False

    strings_match = not query_strings or len(match_strings) == len(query_strings)
    re_patterns_match = not query_re_patterns or len(match_re_patterns) == len(query_re_patterns)
    return strings_match and re_patterns_match


def exclude_file(sp, f: SearchFile):
    """
    Exclude discovered files if they match the special exclude_
    search pattern keys
    """

    # Search by file name (glob)
    for pat in sp.exclude_fn:
        if pat and fnmatch.fnmatch(f.filename, pat):
            return True

    # Search by file name (regex)
    for pat in sp.exclude_fn_re:
        if pat and re.match(pat, f.filename):
            return True

    # Search the contents of the file
    for num_lines, line_block in f.line_block_iterator():
        if sp.exclude_contents:
            for pat in sp.exclude_contents:
                if pat and pat in line_block:
                    return True
        if sp.exclude_contents_re:
            for pat in sp.exclude_contents_re:
                if pat and re.search(pat, line_block):
                    return True
    return False


def data_sources_tofile(data_dir: Path):
    fn = f"multiqc_sources.{config.data_format_extensions[config.data_format]}"
    with io.open(data_dir / fn, "w", encoding="utf-8") as f:
        if config.data_format == "json":
            json.dump(data_sources, f, indent=4, ensure_ascii=False)
        elif config.data_format == "yaml":
            # Unlike JSON, YAML represents defaultdicts as objects, so need to convert
            # them to normal dicts
            yaml.dump(replace_defaultdicts(data_sources), f, default_flow_style=False)
        else:
            lines = [["Module", "Section", "Sample Name", "Source"]]
            for mod in data_sources:
                for sec in data_sources[mod]:
                    for s_name, source in data_sources[mod][sec].items():
                        lines.append([mod, sec, s_name, source])
            body = "\n".join(["\t".join(line) for line in lines])
            print(body.encode("utf-8", "ignore").decode("utf-8"), file=f)


def dois_tofile(data_dir: Path, module_list: List["BaseMultiqcModule"]):
    """Find all DOIs listed in report sections and write to a file"""
    # Collect DOIs
    dois = {"MultiQC": ["10.1093/bioinformatics/btw354"]}
    for mod in module_list:
        if mod.doi is not None and mod.doi != "" and mod.doi != []:
            dois[mod.id] = mod.doi
    # Write to a file
    fn = f"multiqc_citations.{config.data_format_extensions[config.data_format]}"
    with open(data_dir / fn, "w") as f:
        if config.data_format == "json":
            json.dump(dois, f, indent=4, ensure_ascii=False)
        elif config.data_format == "yaml":
            # Unlike JSON, YAML represents defaultdicts as objects, so need to convert
            # them to normal dicts
            yaml.dump(replace_defaultdicts(dois), f, default_flow_style=False)
        else:
            body = ""
            for mod_name, dois_strings in dois.items():
                for doi_string in dois_strings:
                    body += f"{doi_string}{' ' * (50 - len(doi_string))} # {mod_name}\n"
            print(body.encode("utf-8", "ignore").decode("utf-8"), file=f)


def clean_htmlid(html_id: str) -> str:
    """
    Clean up an HTML ID to remove illegal characters.
    """
    # Trailing whitespace
    html_id_clean = html_id.strip()

    # Trailing underscores
    html_id_clean = html_id_clean.strip("_")

    # Must begin with a letter
    if re.match(r"^[a-zA-Z]", html_id_clean) is None:
        html_id_clean = f"mqc_{html_id_clean}"

    # Replace illegal characters
    html_id_clean = re.sub("[^a-zA-Z0-9_-]+", "_", html_id_clean)

    return html_id_clean


def save_htmlid(html_id: str, skiplint: bool = False, scope: Optional[str] = None) -> str:
    """Take a HTML ID, sanitise for HTML, check for duplicates and save.
    Returns sanitised, unique ID"""
    global html_ids_by_scope
    global lint_errors

    # Clean up the HTML ID
    html_id_clean = clean_htmlid(html_id)

    # Validate if linting
    modname = ""
    codeline = ""
    if config.strict and not skiplint:
        callstack = inspect.stack()
        for n in callstack:
            if "multiqc/modules/" in n[1] and "base_module.py" not in n[1]:
                callpath = n[1].split("multiqc/modules/", 1)[-1]
                modname = f">{callpath}< "
                code = n[4]
                if isinstance(code, str):
                    codeline = code[0].strip()
                break
        if html_id != html_id_clean:
            errmsg = f"LINT: {modname}HTML ID was not clean ('{html_id}' -> '{html_id_clean}') ## {codeline}"
            logger.error(errmsg)
            lint_errors.append(errmsg)

    # Check for duplicates
    i = 1
    html_id_base = html_id_clean
    while Anchor(html_id_clean) in html_ids_by_scope[scope]:
        if html_id_clean == "general_stats_table":
            raise ValueError("HTML ID 'general_stats_table' is reserved and cannot be used")
        html_id_clean = f"{html_id_base}-{i}"
        i += 1
        if config.strict and not skiplint:
            errmsg = f"LINT: {modname}HTML ID was a duplicate ({html_id_clean}) ## {codeline}"
            logger.error(errmsg)
            lint_errors.append(errmsg)

    # Remember and return
    html_ids_by_scope[scope].add(Anchor(html_id_clean))
    return html_id_clean


def compress_json(data):
    """
    Take a Python data object. Convert to JSON and compress using gzip.
    Represent in base64 format.
    """
    # Stream to an in-memory buffer rather than compressing the big string
    # at once. This saves memory.
    buffer = io.BytesIO()
    with gzip.open(buffer, "wt", encoding="utf-8", compresslevel=6) as gzip_buffer:
        # The compression level 6 gives 10% speed gain vs. 2% extra size, in contrast to default compresslevel=9
        dump_json(data, gzip_buffer)
    base64_bytes = base64.b64encode(buffer.getvalue())
    return base64_bytes.decode("ascii")


def write_data_file(
    data: Union[
        Mapping[str, Any],
        Sequence[Mapping[str, Any]],
        Sequence[Sequence[Any]],
    ],
    fn: str,
    sort_cols: bool = False,
    data_format: Optional[str] = None,
):
    """
    Write a data file to the report directory. Will do nothing
    :param: data - either: a 2D dict, first key sample name (row header),
        second key field (column header); a list of dicts; or a list of lists
    :param: fn - Desired filename. Directory will be prepended automatically.
    :param: sort_cols - Sort columns alphabetically
    :param: data_format - Output format. Defaults to config.data_format (usually tsv)
    :return: None
    """

    if not config.make_data_dir or config.filename == "stdout":
        return

    # Get data format from config
    if data_format is None:
        data_format = config.data_format

    body = None
    # Some metrics can't be coerced to tab-separated output, test and handle exceptions
    if data_format in ["tsv", "csv"]:
        sep = "\t" if data_format == "tsv" else ","
        # Attempt to reshape data to tsv
        # noinspection PyBroadException
        try:
            # Get all headers from the data, except if data is a dictionary (i.e. has >1 dimensions)
            header_set = set()
            headers = []
            rows = []

            for d in data.values() if isinstance(data, dict) else data:
                if not d or (isinstance(d, list) and isinstance(d[0], dict)):
                    continue
                if isinstance(d, dict):
                    for h in d.keys():
                        if h not in header_set:  # Use a set for fast membership checking.
                            header_set.add(h)
                            headers.append(h)
            if headers:
                if sort_cols:
                    headers = sorted(headers)
                headers_str = [str(item) for item in headers]
                if isinstance(data, dict):
                    # Add Sample header as a first element
                    headers_str.insert(0, "Sample")
                rows.append(sep.join(headers_str))

            # The rest of the rows
            for key, d in sorted(data.items()) if isinstance(data, dict) else enumerate(data):  # type: ignore
                # Make a list starting with the sample name, then each field in order of the header cols
                if headers:
                    assert isinstance(d, dict)
                    line = [str(d.get(h, "")) for h in headers]
                else:
                    line = [
                        str(item)
                        for item in (d.values() if isinstance(d, dict) else (d if isinstance(d, list) else [d]))
                    ]
                if isinstance(data, dict):
                    # Add Sample header as a first element
                    line.insert(0, str(key))
                rows.append(sep.join(line))
            body = "\n".join(rows)

        except Exception as e:
            if config.development:
                raise
            data_format = "yaml"
            logger.debug(f"{fn} could not be saved as tsv/csv, falling back to YAML. {e}")

    # Add relevant file extension to filename, save file.
    fn = f"{fn}.{config.data_format_extensions[data_format]}"
    fpath = data_tmp_dir() / fn
    assert data_tmp_dir() and data_tmp_dir().exists()
    with open(fpath, "w", encoding="utf-8", errors="ignore") as f:
        if data_format == "json":
            dump_json(data, f, indent=4, ensure_ascii=False)
        elif data_format == "yaml":
            yaml.dump(replace_defaultdicts(data), f, default_flow_style=False)
        elif body:
            # Default - tab separated output
            print(body.encode("utf-8", "ignore").decode("utf-8"), file=f)
    logger.debug(f"Wrote data file {fn}")


def multiqc_dump_json(data_dir: Path):
    """
    Export the parsed data in memory to a JSON file.
    Upload to MegaQC if requested.
    WARNING: May be depreciated and removed in future versions.
    """
    exported_data: Dict[str, Any] = dict()
    export_vars = {
        "report": [
            "data_sources",
            "general_stats_data",
            "general_stats_headers",
            "multiqc_command",
            "plot_data",
            "creation_date",
        ],
        "config": [
            "analysis_dir",
            "git_hash",
            "intro_text",
            "report_comment",
            "report_header_info",
            "script_path",
            "short_version",
            "subtitle",
            "title",
            "version",
            "output_dir",
        ],
    }
    for pymod, names in export_vars.items():
        for name in names:
            try:
                d = None
                if pymod == "config":
                    val: Any = getattr(config, name)
                    val = str(val) if isinstance(val, PosixPath) else val
                    if isinstance(val, list):
                        val = [str(el) if isinstance(el, PosixPath) else el for el in val]
                    d = {f"{pymod}_{name}": val}
                elif pymod == "report":
                    val = getattr(sys.modules[__name__], name)
                    if name == "general_stats_data":
                        # List[Dict[SampleGroup, List[InputRow]]]
                        # flattening sample groups for export
                        flattened_sections: List[Dict[SampleName, Dict[ColumnKey, Optional[ValueT]]]] = []
                        for section in general_stats_data:
                            fl_sec: Dict[SampleName, Dict[ColumnKey, Optional[ValueT]]] = dict()
                            for _, rows in section.items():
                                if isinstance(rows, list):
                                    for row in rows:
                                        fl_sec[row.sample] = row.data
                                else:
                                    fl_sec = rows  # old format without grouping, in case if use plugins override it
                            flattened_sections.append(fl_sec)
                        val = flattened_sections
                    elif name == "creation_date":
                        val = creation_date.strftime("%Y-%m-%d, %H:%M %Z")
                    d = {f"{pymod}_{name}": val}
                if d:
                    with open(os.devnull, "wt") as f:
                        # Test that exporting to JSON works. Write to
                        # /dev/null so no memory is required.
                        dump_json(d, f, ensure_ascii=False)
                    exported_data.update(d)
            except (TypeError, KeyError, AttributeError) as e:
                logger.warning(f"Couldn't export data key '{pymod}.{name}': {e}")

    # Get the absolute paths of analysis directories
    config_analysis_dir_abs: List[str] = list()
    for config_analysis_dir in exported_data.get("config_analysis_dir", []):
        try:
            config_analysis_dir_abs.append(str(os.path.abspath(config_analysis_dir)))
        except Exception:
            pass
    exported_data["config_analysis_dir_abs"] = config_analysis_dir_abs

    # Write to file
    out_path = data_dir / "multiqc_data.json"
    if config.data_dump_file:
        with out_path.open("w") as f:
            dump_json(exported_data, f, indent=4, ensure_ascii=False)

    # Add raw data - but instead of all in one, write key by key to avoid loading all in memory
    if config.data_dump_file_write_raw or config.megaqc_url:
        try:
            # Instead of storing in exported_data, write directly to file
            with open(out_path, "rb+") as fh:
                # Move cursor to end of file
                fh.seek(0, 2)
                # Move back until we find the last }
                pos = fh.tell()
                while pos > 0:
                    pos -= 1
                    fh.seek(pos)
                    char = fh.read(1)
                    if char == b"}":
                        fh.seek(pos - 1)
                        fh.truncate()
                        break

            # Now append the new data
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write(',\n    "report_saved_raw_data": {\n')

                # Write each key's data individually
                for i, key in enumerate(saved_raw_data_keys):
                    if (json_path := data_dir / f"{key}.json").exists():
                        fh.write(f'        "{key}": ')
                        # Stream the contents of the individual JSON file
                        with open(json_path, "r", encoding="utf-8") as single_fh:
                            content = single_fh.read().strip()
                            # Indent the content
                            content = content.replace("\n", "\n        ")
                            # Write the content
                            fh.write(content)
                        # Add comma if not last item
                        if i < len(saved_raw_data_keys) - 1:
                            fh.write(",\n")
                        else:
                            fh.write("\n")

                        # Clean up individual JSON file if no longer needed
                        json_path.unlink()

                # Close the JSON object
                fh.write("    }\n")
                fh.write("}")
        except Exception as e:
            logger.warning(f"Couldn't write raw data to JSON file: {e}")

        if config.megaqc_url:
            megaqc.multiqc_api_post(out_path)


def get_all_sections() -> List[Section]:
    return [s for mod in modules for s in mod.sections if not mod.hidden]


def remove_tmp_dir():
    """
    Completely remove tmp dir
    """
    log_and_rich.remove_file_handler()
    rmtree_with_retries(tmp_dir.get_tmp_dir())
    tmp_dir.new_tmp_dir()


def reset_tmp_dir():
    """
    Re-create tmp dir
    """
    remove_tmp_dir()
    tmp_dir.get_tmp_dir()


def add_ai_summary():
    ai.add_ai_summary_to_report()


def anonymize_sample_name(sample: str) -> str:
    """
    Anonymise sample name - sample can be any key in a plot, like SAMPLE1, SAMPLE1-SAMPLE2, etc.
    Method will attempt to replace SAMPLE1 exactly, and more complex cases with text-replace
    which can be inacurate.
    """
    if not config.ai_anonymize_samples or not ai_pseudonym_map:
        return sample

    # Exact match
    if SampleName(sample) in ai_pseudonym_map:
        return ai_pseudonym_map[SampleName(sample)]

    # Try replacing partial matches for cases like sample="SAMPLE1-SAMPLE2"
    # Start with the longest original name ro avoid situations when one sample is a prefix of another
    for original, pseudonym in sorted(ai_pseudonym_map.items(), key=lambda x: len(x[1]), reverse=True):
        if original in sample:
            sample = sample.replace(original, pseudonym)
    return sample
