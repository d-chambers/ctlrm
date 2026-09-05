"""Workflow graph parsing and validation."""

from __future__ import annotations

from ctlrm.runtime.documents import load_yaml

from pydantic import BaseModel, Field


class WorkflowNode(BaseModel):
    """A node assigned to a workflow participant."""

    participant: str


class WorkflowEdge(BaseModel):
    """A transition between workflow nodes."""

    from_: str = Field(alias="from")
    to: str
    when: str


class WorkflowGraph(BaseModel):
    """Directed workflow graph used to route participant work."""

    id: str
    title: str
    nodes: dict[str, WorkflowNode]
    edges: list[WorkflowEdge] = Field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> WorkflowGraph:
        """Parse and validate a workflow graph from YAML text."""
        workflow = cls.model_validate(load_yaml(text))
        workflow.validate_graph()
        return workflow

    def validate_graph(self) -> None:
        """Validate that all workflow edges reference known nodes."""
        for edge in self.edges:
            if edge.from_ not in self.nodes:
                raise ValueError(f"edge references unknown from node {edge.from_}")
            if edge.to != "done" and edge.to not in self.nodes:
                raise ValueError(f"edge references unknown to node {edge.to}")
