from __future__ import annotations

import yaml
from pydantic import BaseModel, Field


class WorkflowNode(BaseModel):
    participant: str


class WorkflowEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    when: str


class WorkflowGraph(BaseModel):
    id: str
    title: str
    nodes: dict[str, WorkflowNode]
    edges: list[WorkflowEdge] = Field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> WorkflowGraph:
        workflow = cls.model_validate(yaml.safe_load(text))
        workflow.validate_graph()
        return workflow

    def validate_graph(self) -> None:
        for edge in self.edges:
            if edge.from_ not in self.nodes:
                raise ValueError(f"edge references unknown from node {edge.from_}")
            if edge.to != "done" and edge.to not in self.nodes:
                raise ValueError(f"edge references unknown to node {edge.to}")
