"""Reject incomplete releases while keeping editable development installs lightweight."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if version != "editable" and not (Path(self.root) / "web/dist/index.html").is_file():
            raise FileNotFoundError(
                "web/dist/index.html is missing; run `bun install --frozen-lockfile` "
                "and `bun run build` in web/ before building a release"
            )
