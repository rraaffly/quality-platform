from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field


class ParsedBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    text: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    headings: tuple[str, ...] = ()


_BLOCK_KINDS = {
    "heading_open": "heading",
    "paragraph_open": "paragraph",
    "table_open": "table",
    "bullet_list_open": "list",
    "ordered_list_open": "list",
    "blockquote_open": "blockquote",
    "fence": "code",
    "code_block": "code",
    "html_block": "html",
    "hr": "rule",
}


def parse_markdown(text: str, source_id: str) -> list[ParsedBlock]:
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(text)
    lines = text.splitlines()
    headings: list[str] = []
    blocks: list[ParsedBlock] = []
    for index, token in enumerate(tokens):
        if token.level != 0 or token.map is None or token.type not in _BLOCK_KINDS:
            continue
        start, end = token.map
        if token.type == "heading_open":
            heading_level = int(token.tag.removeprefix("h"))
            title = tokens[index + 1].content.strip()
            headings = headings[: heading_level - 1] + [title]
        blocks.append(
            ParsedBlock(
                source_id=source_id,
                kind=_BLOCK_KINDS[token.type],
                text="\n".join(lines[start:end]),
                start_line=start + 1,
                end_line=end,
                headings=tuple(headings),
            )
        )
    return blocks
