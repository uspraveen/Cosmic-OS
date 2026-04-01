---
name: d2
description: D2 diagram language — declarative diagrams for architecture, system design, network topology, database schemas, and layered infrastructure diagrams. Renders to SVG/PNG via d2 CLI. Supports sketch mode.
tags: [d2, architecture, system-design, network, topology, database, infrastructure, svg]
---

# D2 Diagram Skill

## When to Use

Use D2 for:
- **Architecture diagrams** — system components, microservices, data flow
- **Network topology** — VPCs, subnets, servers, load balancers
- **Database schemas** — table relationships with cardinality
- **API flows** — layered request/response with nested containers
- **Cloud infrastructure** — AWS/GCP/Azure resource diagrams
- **Component diagrams** — showing hierarchy and containment

D2 is best when you need **nested containers**, **clear hierarchy**, and **structured layout**. It handles complexity better than Mermaid for large system diagrams.

## Syntax Reference

### Basic Nodes and Connections

```d2
server: "Web Server" {
  shape: rectangle
}

database: "PostgreSQL" {
  shape: cylinder
}

server -> database: "SQL queries" {
  style.stroke-dash: 3
}
```

### Containers / Nested Groups

```d2
cloud: "AWS" {
  vpc: "VPC 10.0.0.0/16" {
    public: "Public Subnet" {
      lb: "ALB" {
        shape: hexagon
      }
      bastion: "Bastion" {
        shape: diamond
      }
    }
    private: "Private Subnet" {
      app: "App Servers" {
        shape: rectangle
      }
      cache: "Redis" {
        shape: cylinder
      }
    }
  }
}

cloud.vpc.public.lb -> cloud.vpc.private.app: "HTTP"
cloud.vpc.private.app -> cloud.vpc.private.cache: "TCP 6379"
```

### Database Schema

```d2
users: {
  shape: sql_table
  id: int {constraint: primary_key}
  email: varchar
  name: varchar
  created_at: timestamp
}

orders: {
  shape: sql_table
  id: int {constraint: primary_key}
  user_id: int {constraint: foreign_key}
  total: decimal
  status: varchar
  created_at: timestamp
}

orders.user_id -> users.id
```

### Connections with Styling

```d2
api -> service1: "REST" {
  style.stroke: "#2ecc71"
  style.stroke-width: 2
}
api -> service2: "gRPC" {
  style.stroke: "#3498db"
  style.stroke-width: 2
  style.stroke-dash: 5
}
service1 <-> service2: "sync" {
  style.animated: true
}
```

### Shapes

```d2
box: "Default box"
circle_node: {shape: circle}
diamond_node: {shape: diamond}
hex_node: {shape: hexagon}
cyl_node: {shape: cylinder}
cloud_node: {shape: cloud}
person: {shape: person}
queue: {shape: queue}
pipe: {shape: pipe}
doc: {shape: document}
step: {shape: step}
stored_data: {shape: stored_data}
```

### Directions and Layout

```d2
direction: right  # top, bottom, left, right

# Or set globally at top level
vars: {
  d2-config: {
    layout-engine: elk  # dagre (default) or elk
    theme-id: 1         # 0-8 color themes
  }
}
```

### Styling

```d2
server: {
  style.fill: "#f8f9fa"
  style.stroke: "#2c3e50"
  style.stroke-width: 2
  style.border-radius: 8
  style.font-size: 14
  style.font-color: "#2c3e50"
}

# Dark theme example
vars: {
  d2-config: {
    theme-id: 200  # dark theme
  }
}
```

### Icons

```d2
aws_lambda: {
  icon: https://icons.terrastruct.com/aws%2FCompute%2FAWS-Lambda.svg
}
```

## Rendering

Rendered via `d2` CLI. Outputs SVG or PNG.
- Install: `go install oss.terrastruct.com/d2@latest`
- Command: `d2 input.d2 output.svg --pad 100`
- Sketch mode: `d2 input.d2 output.svg --sketch` (hand-drawn aesthetic)
- Dark theme: Set `theme-id: 200` in vars

## Best Practices

1. Use containers to show hierarchy — don't flatten complex systems
2. Use `sql_table` shape for database schemas — D2 handles column rendering
3. Use `direction: right` for architecture flows, `direction: down` for layered diagrams
4. Add style colors to distinguish component types (frontend=blue, backend=green, data=orange)
5. Use `--sketch` flag for presentation diagrams (whiteboard feel)
6. Keep label text concise — use tooltips for longer descriptions
7. Use `elk` layout engine for complex diagrams (better spacing than `dagre`)
