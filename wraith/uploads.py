"""File upload policy: which local files an agent browser may put into a page.

An upload sends a local file to a website. A prompt injection in page text
could ask the agent to upload ``~/.ssh/id_ed25519``, so uploads are off until
the caller names the folders they may come from. A file must resolve (after
symlinks) to a regular file inside one of those folders.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Union

from ._listspec import ListSpec

__all__ = ["UploadNotAllowedError", "UploadPolicy"]


class UploadNotAllowedError(PermissionError):
    """A file upload is off, or the file is outside the allowed folders."""


class UploadPolicy(ListSpec):
    """Folders an agent browser may upload files from.

    :meth:`parse` takes ``"dir1:dir2"`` (``os.pathsep``) or a list, and
    returns ``None`` when it names no folder: uploads stay off.
    """

    SEPARATOR = os.pathsep

    def __init__(self, folders: Iterable[Union[str, os.PathLike]]) -> None:
        roots = []
        for folder in folders:
            text = str(folder).strip()
            if not text:
                continue
            root = Path(text).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"upload folder {text!r} is not a directory")
            roots.append(root)
        if not roots:
            raise ValueError("the upload folder list is empty")
        self.folders: tuple[Path, ...] = tuple(roots)

    def check(
        self, paths: Union[str, os.PathLike, Iterable[Union[str, os.PathLike]]]
    ) -> list[str]:
        """Return the resolved paths, or raise :class:`UploadNotAllowedError`."""
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        resolved = []
        for path in paths:
            try:
                real = Path(path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise UploadNotAllowedError(f"{path}: the file does not exist") from exc
            if not real.is_file():
                raise UploadNotAllowedError(f"{path}: not a regular file")
            if not any(real.is_relative_to(root) for root in self.folders):
                raise UploadNotAllowedError(
                    f"{path} is outside the upload folders "
                    f"({', '.join(str(root) for root in self.folders)})"
                )
            resolved.append(str(real))
        if not resolved:
            raise UploadNotAllowedError("no file to upload")
        return resolved


def require_policy(policy: Optional[UploadPolicy]) -> UploadPolicy:
    """Return ``policy``, or raise because uploads are off."""
    if policy is None:
        raise UploadNotAllowedError(
            "File upload is off. Allow a folder with AgentBrowser(upload_dirs=...), "
            "`wraith mcp --upload-dir DIR`, or WRAITH_UPLOAD_DIRS."
        )
    return policy
