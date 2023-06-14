"""Generate release notes automatically from GitHub pull requests."""


import os
import re
import sys
import argparse
import logging
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Union
from collections.abc import Iterable

import requests_cache
from tqdm import tqdm
from github import Github, Repository, PullRequest, NamedUser, Commit


logger = logging.getLogger(__name__)

here = Path(__file__).parent

REQUESTS_CACHE_PATH = Path(tempfile.gettempdir()) / "github_cache.sqlite"

GH_URL = "https://github.com"
GH_ORG = "scikit-image"
GH_REPO = "scikit-image"


def lazy_tqdm(*args, **kwargs):
    """Defer initialization of progress bar until first item is requested.

    Calling `tqdm(...)` prints the progress bar right there and then. This can scramble
    output, if more than one progress bar are initialized at the same time but their
    iteration is meant to be done later in successive order.
    """
    kwargs["file"] = kwargs.get("file", sys.stderr)
    yield from tqdm(*args, **kwargs)


def commits_between(repo: Repository, start_rev: str, stop_rev: str) -> set[Commit]:
    """Fetch commits between two revisions excluding the commit of `start_rev`."""
    # https://docs.github.com/en/rest/commits/commits?apiVersion=2022-11-28#compare-two-commits
    comparison = repo.compare(base=start_rev, head=stop_rev)
    return set(comparison.commits)


def pull_requests_from_commits(commits: Iterable[Commit]) -> set[PullRequest]:
    """Fetch pull requests that are associated with the given `commits`."""
    all_pull_requests = set()
    for commit in commits:
        commit_pull_requests = list(commit.get_pulls())
        if len(commit_pull_requests) != 1:
            logger.info(
                "commit %s with no or multiple PR(s): %r",
                commit.html_url,
                [p.html_url for p in commit_pull_requests],
            )
        if any(not p.merged for p in commit_pull_requests):
            logger.error(
                "commit %s with unmerged PRs: %r",
            )
        for pull in commit_pull_requests:
            if pull in all_pull_requests:
                # May happen if
                logger.info(
                    "pull request associated with multiple commits: %r",
                    pull.html_url,
                )
        all_pull_requests.update(commit_pull_requests)
    return all_pull_requests


@dataclass(frozen=True, eq=True)
class UnknownCoAuthor:
    """Represents a co-author for which the GitHub user is not known.

    Hashing and comparing only takes into account the `name` attribute.
    """

    name: str
    email: str = field(compare=False)
    commit: Commit = field(compare=False)


def _find_coauthors(commit: Commit) -> set[UnknownCoAuthor]:
    co_author_regex = re.compile(
        r"^\s*Co-authored-by: (?P<name>[^<]+) <(?P<email>[^>]+)>$", flags=re.MULTILINE
    )
    message = commit.commit.message
    matches = co_author_regex.finditer(message)
    matches = list(matches)
    coauthors = {
        UnknownCoAuthor(name=m["name"], email=m["email"], commit=commit)
        for m in matches
    }
    return coauthors


def contributors(
    commits: Iterable[Commit], pull_requests
) -> tuple[set[NamedUser], set[Union[NamedUser, UnknownCoAuthor]], set[NamedUser]]:
    """Fetch code authors and reviewers.

    `authors` are first authors of commits; co-authors are not included (yet).
    `reviewers` are users, who added reviews to a merged pull request or create the
    merge commit for one.
    """
    authors = set()
    reviewers = set()
    unknown_coauthors = set()

    for commit in commits:
        if commit.author:
            authors.add(commit.author)
        if commit.committer:
            reviewers.add(commit.committer)
        unknown_coauthors.update(_find_coauthors(commit))

    for pull in pull_requests:
        for review in pull.get_reviews():
            if review.user:
                reviewers.add(review.user)

    # Try to replace unknown coauthors with known users by matching email
    coauthors = set()
    known_users_by_mail = {u.email: u for u in authors | reviewers}
    for unknown in unknown_coauthors:
        if unknown.email in known_users_by_mail:
            coauthors.add(known_users_by_mail[unknown.email])
        else:
            coauthors.add(unknown)

    return authors, coauthors, reviewers


@dataclass(frozen=True, kw_only=True)
class MdFormatter:
    """Format release notes in Markdown from PRs, authors and reviewers."""

    pull_requests: set[PullRequest]
    authors: set[NamedUser]
    coauthors: set[Union[NamedUser, UnknownCoAuthor]]
    reviewers: set[NamedUser]

    version: str = "x.y.z"
    title_template: str = "scikit-image {version} release notes"
    intro_template: str = """
We're happy to announce the release of scikit-image {version}!
scikit-image is an image processing toolbox for SciPy that includes algorithms
for segmentation, geometric transformations, color space manipulation,
analysis, filtering, morphology, feature detection, and more.

For more information, examples, and documentation, please visit our website:
https://scikit-image.org

"""
    label_section_map: tuple[str, str] = (
        (":trophy: type: Highlight", "Highlights"),
        (":baby: type: New feature", "New Features"),
        (":fast_forward: type: Enhancement", "Enhancements"),
        (":chart_with_upwards_trend: type: Performance", "Performance"),
        (":adhesive_bandage: type: Bug fix", "Bug Fixes"),
        (":scroll: type: API", "API Changes"),
        (":wrench: type: Maintenance", "Maintenance"),
        (":page_facing_up: type: Documentation", "Documentation"),
        (":robot: type: Infrastructure", "Infrastructure"),
    )
    ignored_user_logins: tuple[str] = ("web-flow",)
    pr_summary_regex = re.compile(
        r"^```release-note\s*(?P<summary>[\s\S]*?\w[\s\S]*?)\s*^```", flags=re.MULTILINE
    )

    def __str__(self) -> str:
        """Return complete release notes document as a string."""
        return "".join(self)

    def __iter__(self) -> Iterable[str]:
        """Iterate the release notes document line-wise."""
        yield from self._format_section_title(
            self.title_template.format(version=self.version), 1
        )
        yield self.intro_template.format(version=self.version)
        for title, pull_requests in self._prs_by_section.items():
            yield from self._format_pr_section(title, pull_requests)
        yield from self._format_contributor_section(
            self.authors, self.coauthors, self.reviewers
        )

    @property
    def document(self) -> str:
        """Return complete release notes document as a string."""
        return str(self)

    def iter_lines(self) -> Iterable[str]:
        """Iterate the release notes document line-wise."""
        return self

    @property
    def _prs_by_section(self) -> dict[str, set[PullRequest]]:
        """Map pull requests to section titles.

        Pull requests that lack a label which is associated with a section, are sorted
        into a section named "Other".
        """
        label_section_map = {k: v for k, v in self.label_section_map}
        prs_by_section = {
            section_name: set() for section_name in label_section_map.values()
        }
        prs_by_section["Other"] = set()
        for pr in self.pull_requests:
            pr_labels = {label.name for label in pr.labels}
            pr_labels = pr_labels & label_section_map.keys()
            if not pr_labels:
                logger.warning(
                    "pull request %s without known section label, sorting into 'Other'",
                    pr.html_url,
                )
                prs_by_section["Other"].add(pr)
            for name in pr_labels:
                prs_by_section[label_section_map[name]].add(pr)

        return prs_by_section

    def _sanitize_text(self, text: str) -> str:
        text = text.strip()
        text = text.replace("\r\n", " ")
        text = text.replace("\n", " ")
        return text

    def _format_link(self, name: str, target: str) -> str:
        return f"[{name}]({target})"

    def _format_section_title(self, title: str, level: int) -> Iterable[str]:
        yield f"{'#' * level} {title}\n"

    def _parse_pull_request_summary(self, pr: PullRequest) -> str:
        if pr.body and (match := self.pr_summary_regex.search(pr.body)):
            summary = match["summary"]
        else:
            logger.debug("falling back to title for %s", pr.html_url)
            summary = pr.title
        summary = self._sanitize_text(summary)
        return summary

    def _format_pull_request(self, pr: PullRequest) -> Iterable[str]:
        summary = self._parse_pull_request_summary(pr).rstrip(".")
        yield f"- {summary}\n"
        link = self._format_link(f"#{pr.number}", f"{pr.html_url}")
        yield f"  ({link}).\n"

    def _format_pr_section(
        self, title: str, pull_requests: set[PullRequest]
    ) -> Iterable[str]:
        """Format a section title and list its pull requests sorted by merge date."""
        yield from self._format_section_title(title, 2)
        for pr in sorted(pull_requests, key=lambda pr: pr.merged_at):
            yield from self._format_pull_request(pr)
        yield "\n"

    def _format_user_line(self, user: Union[NamedUser, UnknownCoAuthor]) -> str:
        if isinstance(user, UnknownCoAuthor):
            line = self._format_link(user.name, user.commit.html_url)
        else:
            line = f"@{user.login}"
            if user.name:
                line = f"{user.name} ({line})"
            line = self._format_link(line, user.html_url)
        return line + ",\n"

    def _format_contributor_section(
        self,
        authors: set[NamedUser],
        coauthors: set[Union[NamedUser, UnknownCoAuthor]],
        reviewers: set[NamedUser],
    ) -> Iterable[str]:
        """Format contributor section and list users sorted by login handle."""
        authors = {u for u in authors if u.login not in self.ignored_user_logins}
        reviewers = {u for u in reviewers if u.login not in self.ignored_user_logins}

        yield from self._format_section_title("Contributors", 2)

        yield f"{len(authors)} authors added to this release (alphabetically):\n"
        author_lines = map(self._format_user_line, authors)
        yield from sorted(author_lines)
        yield "\n"

        yield f"{len(coauthors)} co-authors added to this release (alphabetically):\n"
        coauthor_lines = map(self._format_user_line, coauthors)
        yield from sorted(coauthor_lines)
        yield "\n"

        yield f"{len(reviewers)} reviewers added to this release (alphabetically):\n"
        reviewers_lines = map(self._format_user_line, reviewers)
        yield from sorted(reviewers_lines)
        yield "\n"


class RstFormatter(MdFormatter):
    """Format release notes in reStructuredText from PRs, authors and reviewers."""

    def _sanitize_text(self, text) -> str:
        text = super()._sanitize_text(text)
        text = text.replace("`", "``")
        return text

    def _format_link(self, name: str, target: str) -> str:
        return f"`{name} <{target}>`_"

    def _format_section_title(self, title: str, level: int) -> Iterable[str]:
        yield title + "\n"
        underline = {1: "=", 2: "-", 3: "~"}
        yield underline[level] * len(title) + "\n"


def parse_command_line(func: Callable) -> Callable:
    """Define and parse command line options.

    Has no effect if any keyword argument is passed to the underlying function.
    """
    parser = argparse.ArgumentParser(usage=__doc__)
    parser.add_argument(
        "start_rev",
        help="The starting revision (excluded), e.g. the tag of the previous release",
    )
    parser.add_argument(
        "stop_rev",
        help="The stop revision (included), e.g. the 'main' branch or the current "
        "release",
    )
    parser.add_argument(
        "--version",
        default="0.0.0",
        help="Version you're about to release, used title and description of the notes",
    )
    parser.add_argument("--out", help="Write to file, prints to STDOUT otherwise")
    parser.add_argument(
        "--format",
        choices=["rst", "md"],
        default="md",
        help="Choose format, defaults to Markdown",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear cached requests to GitHub's API before running",
    )

    def wrapped(**kwargs):
        if not kwargs:
            kwargs = vars(parser.parse_args())
        return func(**kwargs)

    return wrapped


@parse_command_line
def main(
    *,
    start_rev: str,
    stop_rev: str,
    version: str,
    out: str,
    format: str,
    clear_cache: bool,
):
    requests_cache.install_cache(
        REQUESTS_CACHE_PATH, backend="sqlite", expire_after=3600
    )
    print(f"Using requests cache at {REQUESTS_CACHE_PATH}")
    if clear_cache:
        requests_cache.clear()
        logger.info("cleared requests cache at %s", REQUESTS_CACHE_PATH)

    gh_token = os.environ.get("GH_TOKEN")
    if gh_token is None:
        raise RuntimeError(
            "You need to set the environment variable `GH_TOKEN`. "
            "The token is used to avoid rate limiting, "
            "and can be created at https://github.com/settings/tokens.\n\n"
            "The token does not require any permissions (we only use the public API)."
        )
    gh = Github(gh_token)
    repo = gh.get_repo(f"{GH_ORG}/{GH_REPO}")

    print("Fetching commits...", file=sys.stderr)
    commits = commits_between(repo, start_rev, stop_rev)
    pull_requests = pull_requests_from_commits(
        lazy_tqdm(commits, desc="Fetching pull requests")
    )
    authors, coauthors, reviewers = contributors(
        commits=lazy_tqdm(
            commits,
            desc="Fetching authors",
        ),
        pull_requests=lazy_tqdm(pull_requests, desc="Fetching reviewers"),
    )

    Formatter = {"md": MdFormatter, "rst": RstFormatter}[format]
    formatter = Formatter(
        pull_requests=pull_requests,
        authors=authors,
        coauthors=coauthors,
        reviewers=reviewers,
        version=version,
    )
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as io:
            io.writelines(formatter.iter_lines())
    else:
        print()
        print(formatter.document, file=sys.stdout)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
