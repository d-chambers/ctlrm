"""Shared parsing helpers for Markdown room documents."""

import yaml


def split_front_matter(text: str) -> tuple[str, str]:
    """Split Markdown into YAML front matter and body text."""
    if not text.startswith("---\n"):
        raise ValueError("missing YAML front matter")
    try:
        front_matter, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("front matter is not closed") from exc
    return front_matter, body


def parse_markdown_document(text: str) -> tuple[dict[str, object], str]:
    """Return validated YAML metadata and normalized Markdown body."""
    front_matter, body = split_front_matter(text)
    metadata = load_yaml(front_matter)
    if not isinstance(metadata, dict):
        raise ValueError("YAML front matter must be a mapping")
    if not all(isinstance(key, str) for key in metadata):
        raise ValueError("YAML front matter keys must be strings")
    return metadata, body.strip()


def normalize_timestamp(value: object) -> str:
    """Validate an ISO timestamp and retain its timezone offset."""
    from datetime import datetime

    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value)
    else:
        raise ValueError("timestamp must be an ISO datetime")
    if moment.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return moment.isoformat()


class _UniqueLoader(yaml.SafeLoader):
    """Reject duplicate or non-string mapping keys in protocol YAML."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
        """Build a mapping without accepting ambiguous or coerced keys."""
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("YAML mapping keys must be strings")
            if key in result:
                raise ValueError(f"duplicate YAML key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_yaml(text: str) -> object:
    """Load protocol YAML with unambiguous string mapping keys."""
    return yaml.load(text, Loader=_UniqueLoader)
