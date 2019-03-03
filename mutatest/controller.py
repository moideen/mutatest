"""Trial and job controller.
"""
import ast
import logging
import random
import subprocess

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

from mutatest.cache import remove_existing_cache_files
from mutatest.maker import MutantTrialResult, create_mutation_and_run_trial, get_mutation_targets
from mutatest.optimizers import (
    DEFAULT_COVERAGE_FILE,
    CoverageOptimizer,
    WhoTestsWhat,
    covered_sample_space,
)
from mutatest.transformers import LocIndex, get_ast_from_src, get_mutations_for_target


LOGGER = logging.getLogger(__name__)


class BaselineTestException(Exception):
    """Used as an exception for the clean trial runs."""


class ResultsSummary(NamedTuple):
    """Results summary container."""

    results: List[MutantTrialResult]
    n_locs_mutated: int
    n_locs_identified: int
    total_runtime: timedelta


def colorize_output(output: str, color: str) -> str:
    """Color output for the terminal display as either red or green.

    Args:
        output: string to colorize
        color: choice of terminal color, "red" vs. "green"

    Returns:
        colorized string, or original string for bad color choice.
    """
    colors = {
        "red": f"\033[91m{output}\033[0m",  # Red text
        "green": f"\033[92m{output}\033[0m",  # Green text
        "yellow": f"\033[93m{output}\033[0m",  # Yellow text
        "blue": f"\033[94m{output}\033[0m",  # Blue text
    }

    return colors.get(color, output)


def is_test_file(src_file: Path) -> bool:
    """Utility function to match the test prefix or suffix in the stem. Ref:

    https://docs.pytest.org/en/latest/goodpractices.html#conventions-for-python-test-discovery

    Args:
        src_file: Path object for the source file in question

    Returns:
        bool status of being a test file
    """

    return src_file.stem.startswith("test_") or src_file.stem.endswith("_test")


def get_py_files(src_loc: Union[str, Path]) -> List[Path]:
    """Find all .py files in src_loc and return absolute path.

    Strings are coerced to Path objects, meaning '' will become ``Path('.')``.

    Args:
        src_loc: the source location to scan, can be file or folder

    Returns:
        List of absolute paths to .py file(s)

    Raises:
        FileNotFoundError, if src_loc is not a valid file or directory
    """
    # ensure Path object in case str is passed
    # but also check that not an empty string, '' is coerced to '.' otherwise.
    if not isinstance(src_loc, Path):
        src_loc = Path(src_loc)

    # in case a single py file is passed
    if src_loc.is_file() and src_loc.suffix == ".py" and not is_test_file(src_loc):
        return [src_loc.resolve()]

    # if a directory is passed
    if src_loc.is_dir():
        relative_list = list(src_loc.rglob("*.py"))
        return [p.resolve() for p in relative_list if not is_test_file(p)]

    raise FileNotFoundError(f"{src_loc} is not a valid Python file or directory.")


def clean_trial(src_loc: Path, test_cmds: List[str]) -> timedelta:
    """Remove all existing cache files and run the test suite.

    Args:
        src_loc: the directory of the package for cache removal, may be a file
        test_cmds: test running commands for subprocess.run()

    Returns:
        None

    Raises:
        Exception if the clean trial does not pass from the test run.
    """
    remove_existing_cache_files(src_loc)

    LOGGER.info("Running clean trial")

    # clean trial will show output all the time for diagnostic purposes
    start = datetime.now()
    clean_run = subprocess.run(test_cmds, capture_output=False)
    end = datetime.now()

    if clean_run.returncode != 0:
        raise BaselineTestException(
            f"Clean trial does not pass, mutant tests will be meaningless.\n"
            f"Output: {clean_run.stdout}"
        )

    return end - start


def build_src_trees_and_targets(
    src_loc: Union[str, Path], exclude_files: Optional[List[Path]] = None
) -> Tuple[Dict[str, ast.Module], Dict[str, List[LocIndex]]]:
    """Build the source AST references and find all mutatest target locations for each.

    Args:
        src_loc: the source code package directory to scan or file location

    Returns:
        Tuple(source trees, source targets)
    """
    src_trees: Dict[str, ast.Module] = {}
    src_targets: Dict[str, List[LocIndex]] = {}

    for src_file in get_py_files(src_loc):

        # if the src_file is in the exclusion list then reset to the next iteration
        if exclude_files:
            if src_file in exclude_files:
                LOGGER.info("%s", colorize_output(f"Exclusion: {src_file}", "yellow"))
                continue

        tree = get_ast_from_src(src_file)
        targets = get_mutation_targets(tree, src_file)
        LOGGER.info(
            "%s",
            colorize_output(
                f"{len(targets)} mutation targets found in {src_file} AST.",
                "green" if len(targets) > 0 else "yellow",
            ),
        )

        # only add files that have at least one valid target for mutatest
        if targets:
            src_trees[str(src_file)] = tree
            src_targets[str(src_file)] = [tgt for tgt in targets]

    return src_trees, src_targets


def get_sample_space(src_targets: Dict[str, List[LocIndex]]) -> List[Tuple[str, LocIndex]]:
    """Create a flat sample space of source files and mutatest targets.

    Args:
        src_targets: Dictionary of targets indexed by source file

    Returns:
        List of source-file and target-index pairs as a flat structure.
    """

    sample_space = []
    for src_file, target_list in src_targets.items():
        for target in target_list:
            sample_space.append((src_file, target))

    return sample_space


def get_mutation_sample_locations(
    sample_space: List[Tuple[str, LocIndex]], n_locations: Optional[int] = None
) -> List[Tuple[str, LocIndex]]:
    """Create the mutation sample space and set n_locations to a correct value for reporting.

    n_locations will change if it is larger than the total sample_space (or is unset).
    If n_locations is not specified the full sample is returned as the mutation sample space.
    This process requires a seed to be set before invocation for repeatable results in the
    random sample.

    Args:
        sample_space: sample space to draw random locations from
        n_locations: number of locations to draw

    Returns:
        mutation sample
    """
    # set the mutation sample to the full sample space
    # then if max_trials is set and less than the size of the sample space
    # take a random sample without replacement
    mutation_sample = sample_space

    # natural Falsey evaluation of n_locations=0 requires exact None check
    if n_locations is not None:
        if n_locations < 0:
            raise ValueError("n_locations must be greater or equal to zero.")

        if n_locations <= len(sample_space):
            LOGGER.info(
                "%s",
                colorize_output(
                    f"Selecting {n_locations} locations from {len(sample_space)} potentials.",
                    "green",
                ),
            )
            mutation_sample = random.sample(sample_space, k=n_locations)

        else:
            # set here for final reporting, though not used in rest of trial controls
            LOGGER.info(
                "%s",
                colorize_output(
                    f"{n_locations} exceeds sample space, using full sample: {len(sample_space)}.",
                    "yellow",
                ),
            )

    return mutation_sample


def optimize_covered_sample(
    sample_space: List[Tuple[str, LocIndex]], cov_file: Optional[Path] = None
) -> List[Tuple[str, LocIndex]]:
    """Optimize the overall sample space to only those marked with coverage.

    Args:
        sample_space: the raw sample space

    Returns:
        The subset list of the sample space that is marked by coverage.
    """
    copt = CoverageOptimizer(cov_file=cov_file)
    covered_sample = covered_sample_space(sample_space, copt.cov_mapping)
    LOGGER.debug("Coverage file mapping:\n%s", copt.cov_mapping)
    LOGGER.info("Coverage optimized sample space size: %s", len(covered_sample))
    return covered_sample


def get_sources_with_sample(
    src_loc: Union[str, Path],
    exclude_files: Optional[List[Path]] = None,
    ignore_coverage: bool = False,
    cov_mapping: Optional[Dict[str, List[int]]] = None,
) -> Tuple[Dict[str, ast.Module], List[Tuple[str, LocIndex]]]:
    """Determines the sample for selecting mutations, which may be restricted by optimizers.

    Args:
        src_loc: source location path for the package to scan
        exclude_files: list of file exclusions
        ignore_coverage: flag to skip any found coverage files and only use raw sample

    Returns:
        Tuple of the source trees in a reference mapping and the sample space list
    """
    src_trees, src_targets = build_src_trees_and_targets(
        src_loc=src_loc, exclude_files=exclude_files
    )
    optimized_sample: List[Tuple[str, LocIndex]] = []
    sample_space = get_sample_space(src_targets)
    LOGGER.info("Full sample space size: %s", len(sample_space))

    # restrict the sample space down to locations marked by coverage
    if not ignore_coverage and cov_mapping is not None:
        LOGGER.info("Restricting sample based on pre-built coverage mapping.")
        optimized_sample = covered_sample_space(sample_space, cov_mapping)

    # if the input_cov mapping is None
    if not ignore_coverage and DEFAULT_COVERAGE_FILE.exists() and cov_mapping is None:
        LOGGER.info("Restricting sample based on existing coverage file.")
        optimized_sample = optimize_covered_sample(sample_space)

    if len(optimized_sample) > 0:
        LOGGER.info("Optimized sample set, size: %s", len(optimized_sample))
        sample_space = optimized_sample

    return src_trees, sample_space


def get_trial_test_cmds(
    test_cmds: List[str], sample_src: str, sample_idx: LocIndex, wtw: Optional[WhoTestsWhat] = None
) -> List[str]:
    """Generate trial test commands with potential wtw deselection.

    Args:
        test_cmds: original test command list
        sample_src: the sample source file
        sample_idx: sample location index
        wtw: optional Who-Tests-What instance

    Returns:
        test commands for the trials
    """

    trial_test_cmds = [t for t in test_cmds]

    if wtw is not None:
        deselect_args, kept_tests = wtw.get_src_line_deselection(sample_src, sample_idx.lineno)
        l_kept = len(kept_tests)
        l_total = l_kept + int(len(deselect_args) / 2)

        if l_kept > 0:
            LOGGER.info(
                "%s",
                colorize_output(
                    f"Who-tests-what: keeping {l_kept}/{l_total} tests for mutation trial.",
                    "yellow",
                ),
            )
            LOGGER.debug("Deselected test count: %s", len(deselect_args) / 2)
            trial_test_cmds.extend(deselect_args)

        else:
            LOGGER.info(
                colorize_output(
                    "Who-tests-what: optimization attempt resulted in 0 tests, "
                    "skipping deselection and running typical trial.",
                    "yellow",
                )
            )

    return trial_test_cmds


def run_mutation_trials(  # noqa: C901
    src_loc: Union[str, Path],
    test_cmds: List[str],
    wtw: Optional[WhoTestsWhat] = None,
    exclude_files: Optional[List[Path]] = None,
    n_locations: Optional[int] = None,
    break_on_survival: bool = False,
    break_on_detected: bool = False,
    break_on_error: bool = False,
    break_on_unknown: bool = False,
    ignore_coverage: bool = False,
) -> ResultsSummary:
    """Run the mutatest trials. This uses random sampling for locations and operations.

    Set a SEED for the pseudo-random number generation before calling this function for
    repeatable trial results.

    Args:
        src_loc: the source file package directory
        test_cmds: the test runner commands for subprocess.run()
        wtw: WhoTestsWhat optimizer instance
        exclude_files: optional list of files to exclude from mutation trials, default None
        n_locations: optional number of locations for mutations,
            if unspecified then the full sample space is used.
        break_on_survival: flag to stop further mutations at a location if one survives,
            defaults to False
        break_on_detected: flag to stop further mutations at a location if one is detected,
            defaults to False
        break_on_error: flag to stop further mutations at a location if there is an error,
            defaults to False
        break_on_unknown: flag to stop further mutations at a location if the status is unknown,
            defaults to False
        ignore_coverage: flag to ignore coverage optimization

    Returns:
        List of mutants and trial results
    """
    # Create the AST for each source file and make potential targets sample space
    start = datetime.now()

    # if who-tests-what optimization is in place
    pre_cov = wtw.cov_mapping if wtw is not None else None

    src_trees, sample_space = get_sources_with_sample(
        src_loc=src_loc,
        exclude_files=exclude_files,
        ignore_coverage=ignore_coverage,
        cov_mapping=pre_cov,
    )

    mutation_sample = get_mutation_sample_locations(
        sample_space=sample_space, n_locations=n_locations
    )

    results: List[MutantTrialResult] = []

    LOGGER.info("Starting individual mutation trials!")
    for sample_src, sample_idx in mutation_sample:

        LOGGER.info("Current target location: %s, %s", Path(sample_src).name, sample_idx)
        mutant_operations = get_mutations_for_target(sample_idx)
        src_tree = src_trees[sample_src]

        trial_test_cmds = get_trial_test_cmds(test_cmds, sample_src, sample_idx, wtw)

        while mutant_operations:
            # random.choice doesn't support sets, but sample of 1 produces a list with one element
            current_mutation = random.sample(mutant_operations, k=1)[0]
            mutant_operations.remove(current_mutation)

            LOGGER.debug("Running trial for %s", current_mutation)

            trial_results = create_mutation_and_run_trial(
                src_tree=src_tree,
                src_file=sample_src,
                target_idx=sample_idx,
                mutation_op=current_mutation,
                test_cmds=trial_test_cmds,
            )

            results.append(trial_results)

            if trial_results.status == "SURVIVED":
                LOGGER.info(
                    "%s",
                    colorize_output(
                        (
                            f"Surviving mutation detected at "
                            f"{sample_src}: ({sample_idx.lineno}, {sample_idx.col_offset})"
                        ),
                        "red",
                    ),
                )
                if break_on_survival:
                    LOGGER.info(
                        "%s",
                        colorize_output(
                            "Break on survival: stopping further mutations at location.", "red"
                        ),
                    )
                    break

            if trial_results.status == "DETECTED":
                LOGGER.info(
                    "%s",
                    colorize_output(
                        (
                            f"Detected mutation at "
                            f"{sample_src}: ({sample_idx.lineno}, {sample_idx.col_offset})"
                        ),
                        "green",
                    ),
                )
                if break_on_detected:
                    LOGGER.info(
                        "%s",
                        colorize_output(
                            "Break on detected: stopping further mutations at location.", "green"
                        ),
                    )
                    break

            if trial_results.status == "ERROR":
                LOGGER.info(
                    "%s",
                    colorize_output(
                        (
                            f"Error with mutation at "
                            f"{sample_src}: ({sample_idx.lineno}, {sample_idx.col_offset})"
                        ),
                        "yellow",
                    ),
                )
                if break_on_error:
                    LOGGER.info(
                        "%s",
                        colorize_output(
                            "Break on error: stopping further mutations at location.", "yellow"
                        ),
                    )
                    break

            if trial_results.status == "UNKNOWN":
                LOGGER.info(
                    "%s",
                    colorize_output(
                        (
                            f"Unknown mutation result at "
                            f"{sample_src}: ({sample_idx.lineno}, {sample_idx.col_offset})"
                        ),
                        "yellow",
                    ),
                )
                if break_on_unknown:
                    LOGGER.info(
                        "%s",
                        colorize_output(
                            "Break on unknown: stopping further mutations at location.", "yellow"
                        ),
                    )
                    break

    end = datetime.now()
    return ResultsSummary(
        results=results,
        n_locs_mutated=len(mutation_sample),
        n_locs_identified=len(sample_space),
        total_runtime=end - start,
    )
