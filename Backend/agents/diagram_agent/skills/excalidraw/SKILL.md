---
name: excalidraw
description: Excalidraw JSON format — hand-drawn whiteboard-style diagrams. Brainstorming, wireframes, informal sketches, and collaborative visual thinking. Output as .excalidraw JSON for desktop rendering.
tags: [excalidraw, whiteboard, sketch, hand-drawn, brainstorm, wireframe, canvas, json]
---

# Excalidraw Diagram Skill

## When to Use

Use Excalidraw for:
- **Brainstorming visuals** — rough ideas, mind maps, concept sketches
- **Wireframes** — UI layout sketches (not pixel-perfect mockups)
- **Informal diagrams** — quick visual explanations for presentations
- **Whiteboard sessions** — collaborative thinking with hand-drawn feel
- **Concept maps** — visual relationships between ideas
- **Architecture sketches** — high-level system overviews before formalizing in D2

Excalidraw is the "casual" renderer. Use it when the user wants something that looks hand-drawn, not polished.

## JSON Format Reference

Excalidraw files are JSON objects with this structure:

```json
{
  "type": "excalidraw",
  "version": 2,
  "source": "cosmic/diagram-agent",
  "elements": [...],
  "appState": {
    "gridSize": null,
    "viewBackgroundColor": "#ffffff"
  },
  "files": {}
}
```

### Element Types

#### Rectangle
```json
{
  "type": "rectangle",
  "id": "rect_1",
  "x": 100,
  "y": 100,
  "width": 200,
  "height": 100,
  "angle": 0,
  "strokeColor": "#1e1e1e",
  "backgroundColor": "#a5d8ff",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "strokeStyle": "solid",
  "roughness": 1,
  "opacity": 100,
  "roundness": { "type": 3 },
  "seed": 123456789,
  "version": 1,
  "versionNonce": 987654321,
  "isDeleted": false,
  "boundElements": null,
  "updated": 1,
  "link": null,
  "locked": false
}
```

#### Text
```json
{
  "type": "text",
  "id": "text_1",
  "x": 120,
  "y": 130,
  "width": 160,
  "height": 25,
  "text": "Hello World",
  "fontSize": 20,
  "fontFamily": 1,
  "textAlign": "center",
  "verticalAlign": "middle",
  "containerId": "rect_1",
  "strokeColor": "#1e1e1e",
  "backgroundColor": "transparent",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "strokeStyle": "solid",
  "roughness": 1,
  "opacity": 100,
  "angle": 0,
  "seed": 111111111,
  "version": 1,
  "versionNonce": 222222222,
  "isDeleted": false,
  "boundElements": null,
  "updated": 1,
  "link": null,
  "locked": false
}
```

#### Arrow / Line
```json
{
  "type": "arrow",
  "id": "arrow_1",
  "x": 300,
  "y": 150,
  "width": 100,
  "height": 0,
  "points": [[0, 0], [100, 0]],
  "startBinding": {
    "elementId": "rect_1",
    "focus": 0,
    "gap": 1
  },
  "endBinding": {
    "elementId": "rect_2",
    "focus": 0,
    "gap": 1
  },
  "startArrowhead": null,
  "endArrowhead": "arrow",
  "strokeColor": "#1e1e1e",
  "backgroundColor": "transparent",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "strokeStyle": "solid",
  "roughness": 1,
  "opacity": 100,
  "angle": 0,
  "seed": 333333333,
  "version": 1,
  "versionNonce": 444444444,
  "isDeleted": false,
  "boundElements": null,
  "updated": 1,
  "link": null,
  "locked": false
}
```

#### Ellipse
```json
{
  "type": "ellipse",
  "id": "ellipse_1",
  "x": 500,
  "y": 200,
  "width": 120,
  "height": 120,
  "strokeColor": "#e03131",
  "backgroundColor": "#ffc9c9",
  "fillStyle": "solid",
  "strokeWidth": 2,
  "roughness": 1,
  "opacity": 100,
  "angle": 0,
  "seed": 555555555,
  "version": 1,
  "versionNonce": 666666666,
  "isDeleted": false
}
```

### Key Fields for All Elements

| Field | Description | Required |
|---|---|---|
| `type` | Element type: rectangle, ellipse, diamond, text, arrow, line, freedraw, image, frame | Yes |
| `id` | Unique identifier | Yes |
| `x`, `y` | Position in canvas | Yes |
| `width`, `height` | Dimensions | Yes |
| `strokeColor` | Outline color hex | Yes |
| `backgroundColor` | Fill color hex or "transparent" | Yes |
| `fillStyle` | "solid", "hachure", "cross-hatch" | Yes |
| `strokeWidth` | Line thickness (1-4 typical) | Yes |
| `roughness` | Hand-drawn roughness (0=clean, 1=normal, 2=very rough) | Yes |
| `opacity` | 0-100 | Yes |
| `angle` | Rotation in radians | Yes |
| `seed` | Random seed for consistent rendering | Yes |
| `version` | Element version | Yes |
| `versionNonce` | Random nonce | Yes |
| `isDeleted` | Soft delete flag | Yes |
| `text` | Text content (text elements only) | Text only |
| `points` | Coordinate points (arrow/line only) | Arrow/line |
| `containerId` | Parent container id (text inside shapes) | Optional |

### Color Palette

**Backgrounds:** `#ffffff`, `#f8f9fa`, `#fff3bf`, `#d0bfff`, `#a5d8ff`, `#b2f2bb`, `#ffc9c9`, `#ffec99`
**Strokes:** `#1e1e1e` (black), `#1971c2` (blue), `#2f9e44` (green), `#e03131` (red), `#f08c00` (orange), `#7048e8` (purple)

## Best Practices

1. Use `roughness: 1` for standard hand-drawn feel
2. Place text elements inside rectangles by setting `containerId`
3. Use `seed` values based on element id hash for deterministic rendering
4. Keep canvas within 0-4000 x/y range for performance
5. Use `fillStyle: "hachure"` for a more sketchy look
6. Connect elements with arrows using `startBinding`/`endBinding`
7. Use frames to group related elements
8. For mind maps, use freehand (`freedraw`) connecting lines
