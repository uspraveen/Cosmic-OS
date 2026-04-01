---
name: mermaid
description: Mermaid diagram syntax — sequence diagrams, flowcharts, ER diagrams, Gantt charts, state diagrams, class diagrams, and more. Renders to SVG/PNG via mmdc CLI. Native GitHub/markdown rendering support.
tags: [mermaid, sequence, flowchart, erd, gantt, state, class, markdown, github, svg]
---

# Mermaid Diagram Skill

## When to Use

Use Mermaid for:
- **Sequence diagrams** — API call flows, request/response patterns, protocol interactions
- **Flowcharts** — decision trees, process flows, algorithm steps
- **ER diagrams** — database entity relationships
- **Gantt charts** — project timelines, sprint planning
- **State diagrams** — state machines, status workflows, lifecycle diagrams
- **Class diagrams** — OOP inheritance, interfaces, associations
- **Git graphs** — branching strategies, release flows
- **Pie charts** — composition breakdowns
- **Quadrant charts** — 2x2 matrix positioning
- **Timelines** — chronological events

Mermaid is the default renderer when no specific style is requested.

## Syntax Reference

### Sequence Diagram

```mermaid
sequenceDiagram
    participant U as User
    participant G as Gateway
    participant O as Orchestrator
    participant A as Agent

    U->>G: Send message
    G->>O: Route via Model Router
    O->>A: Dispatch TaskEnvelope
    A-->>O: Return AgentResult
    O-->>G: Route response
    G-->>U: Deliver message
```

**Key syntax:**
- `participant X as Label` — define participants
- `->>` synchronous call, `-->>` return
- `->` solid line without arrow, `-->` dashed
- `Note over X,Y: text` — annotations
- `alt/else/end` — conditional flows
- `loop N times` — loops
- `par/and` — parallel flows
- `activate X` / `deactivate X` — lifeline activation

### Flowchart

```mermaid
flowchart TD
    A[Start] --> B{Is it raining?}
    B -->|Yes| C[Take umbrella]
    B -->|No| D[Wear sunglasses]
    C --> E[Go outside]
    D --> E
```

**Node shapes:**
- `[text]` — rectangle
- `(text)` — rounded rectangle
- `{text}`` — diamond (decision)
- `((text))` — circle
- `[/text/]` — parallelogram
- `[[text]]` — subroutine

**Directions:** `TD` (top-down), `LR` (left-right), `RL`, `BT`

### ER Diagram

```mermaid
erDiagram
    USER ||--o{ ORDER : places
    ORDER ||--|{ LINE_ITEM : contains
    PRODUCT ||--o{ LINE_ITEM : "referenced in"

    USER {
        int id PK
        string email
        string name
    }
    ORDER {
        int id PK
        date created_at
        float total
    }
```

**Cardinality:**
- `||` exactly one
- `o|` zero or one
- `}|` one or more
- `}o` zero or more

### Gantt Chart

```mermaid
gantt
    title Project Timeline
    dateFormat YYYY-MM-DD
    section Phase 1
    Research :a1, 2026-01-01, 30d
    Design   :a2, after a1, 20d
    section Phase 2
    Build    :b1, after a2, 45d
    Test     :b2, after b1, 15d
```

### State Diagram

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Processing : start
    Processing --> Completed : success
    Processing --> Failed : error
    Failed --> Processing : retry
    Completed --> [*]
```

### Class Diagram

```mermaid
classDiagram
    class Animal {
        +String name
        +int age
        +makeSound() void
    }
    class Dog {
        +fetch() void
    }
    Animal <|-- Dog
```

## Rendering

Rendered via `mmdc` CLI (mermaid-cli). Outputs SVG or PNG.
- Install: `npm i -g @mermaid-js/mermaid-cli`
- Command: `mmdc -i input.mmd -o output.svg -b white -t default`

## Best Practices

1. Use `TD` direction for process flows, `LR` for system diagrams
2. Keep node labels concise (2-4 words)
3. Use consistent naming: `participant G as Gateway` not `participant Gateway`
4. Add `%% comments` for complex diagrams
5. For large diagrams, break into subgraphs
6. Use `linkStyle` for coloring important paths
7. Keep sequence diagrams to <15 participants for readability
