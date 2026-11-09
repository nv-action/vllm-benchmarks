# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.

"""Execute explicit target-image replay actions after validating all COPYs."""

import shlex
from dataclasses import replace
from pathlib import Path, PurePosixPath

from tools.bisect.build_analyzer.base_apply import _current_entry, apply_base_delta, validate_base_application
from tools.bisect.build_analyzer.base_image import BaseImageDelta, BaseImageSnapshot


class ImageReplayExecutor:
    def __init__(self, actions, resolver, architecture, environment, root=Path("/")):
        self.environment = environment
        self.root = root
        self.cwd = root
        self.copies = {}
        snapshots = {}
        for action in actions:
            if not action.kind.startswith("image-"):
                continue
            kind = action.kind.removeprefix("image-")
            if kind not in {
                "copy-from-target-image",
                "run",
                "env",
                "arg",
                "shell",
                "workdir",
                "user",
                "label",
                "cmd",
                "entrypoint",
            }:
                raise RuntimeError(f"unsupported target-image action: {kind}")
            if kind == "user" and action.command not in {"root", "0", "0:0"}:
                raise RuntimeError(f"cannot change replay process user: {action.command}")
            if kind == "env" and (
                not shlex.split(action.command)
                or any("=" not in assignment for assignment in shlex.split(action.command))
            ):
                raise RuntimeError(f"unsupported ENV instruction: {action.command}")
            if kind == "workdir" and (
                not PurePosixPath(action.command).is_absolute() or ".." in PurePosixPath(action.command).parts
            ):
                raise RuntimeError(f"unsupported WORKDIR: {action.command}")
            if kind != "copy-from-target-image":
                continue
            if not action.content_image or not action.artifact or action.artifact != action.destination:
                raise RuntimeError("COPY needs an exact target-image path and matching destination")
            path = PurePosixPath(action.destination)
            if not path.is_absolute() or ".." in path.parts or str(path) == "/":
                raise RuntimeError(f"unsafe COPY destination: {path}")
            if action.content_image not in snapshots:
                snapshots[action.content_image] = resolver.resolve(action.content_image, architecture)
            snapshot = snapshots[action.content_image]
            prefix = str(path).lstrip("/")
            entries = tuple(
                entry for entry in snapshot.files if entry[0] == prefix or entry[0].startswith(prefix + "/")
            )
            if not entries:
                raise RuntimeError(f"target image is missing COPY content: {path}")
            # Docker COPY merges a directory; it does not delete unrelated live files.
            live = tuple((name, *value) for name, _, _ in entries if (value := _current_entry(root / name)) is not None)
            before = BaseImageSnapshot(
                "live COPY destination", "live", snapshot.architecture, live, (), snapshot.archive
            )
            after = replace(snapshot, files=entries, config=())
            previous = {name: (kind, value) for name, kind, value in live}
            changed = tuple(name for name, kind, value in entries if previous.get(name) != (kind, value))
            delta = BaseImageDelta(before, after, changed, ())
            reasons = validate_base_application(delta, root)
            if reasons:
                raise RuntimeError("COPY preflight failed: " + "; ".join(reasons[:10]))
            self.copies[action.id] = delta

    def execute(self, action, run):
        kind = action.kind.removeprefix("image-")
        if kind == "copy-from-target-image":
            apply_base_delta(self.copies[action.id], self.root)
        elif kind == "env":
            assignments = shlex.split(action.command)
            if not assignments or any("=" not in assignment for assignment in assignments):
                raise RuntimeError(f"unsupported ENV instruction: {action.command}")
            for assignment in assignments:
                name, _, value = assignment.partition("=")
                self.environment[name] = value
        elif kind == "workdir":
            path = PurePosixPath(action.command)
            if not path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsupported WORKDIR: {path}")
            self.cwd = self.root / str(path).lstrip("/")
            self.cwd.mkdir(parents=True, exist_ok=True)
        elif kind == "run":
            command = "cd " + shlex.quote(self.cwd.as_posix()) + " && " + action.command
            run(["/bin/bash", "-c", command], self.environment)
        # ARG is embedded in recovered RUN commands. SHELL is also embedded.
        # CMD/ENTRYPOINT describe container startup; this tool starts pytest explicitly.
        elif kind not in {"arg", "shell", "user", "label", "cmd", "entrypoint"}:
            raise RuntimeError(f"unsupported target-image action: {kind}")
