# Fold Bar State Diagram

## Proposed Behavior

The fold bar has two buttons with four possible states:

### State Definitions

| State | Button 1 | Button 2 | Visibility | Description |
|-------|----------|----------|------------|-------------|
| **A** | ▶ | ▶▶ | Nothing visible | Fully folded |
| **B** | ▼ | ▶▶ | First level visible | One level unfolded |
| **C** | ▼ | ▼▼ | All levels visible | Fully unfolded |

**Note**: The state "▶ ▼▼" (first level folded, all levels unfolded) is **impossible** and should never occur.

## State Transitions

```
        ┌─────────────────────────────────────┐
        │         State A (▶ ▶▶)             │
        │      Nothing visible               │
        └─────────────────────────────────────┘
                 │             │
    Click ▶      │             │      Click ▶▶
   (unfold 1)    │             │      (unfold all)
                 ▼             ▼
        ┌─────────────┐   ┌─────────────┐
        │  State B    │   │  State B    │
        │  (▼ ▶▶)     │◄──┤  (▼ ▶▶)     │
        │  First      │   │  First      │
        │  level      │   │  level      │
        │  visible    │   │  visible    │
        └─────────────┘   └─────────────┘
                 │             │
    Click ▶▶     │             │      Click ▼
   (unfold all)  │             │      (fold 1)
                 ▼             ▼
        ┌─────────────┐   ┌─────────────┐
        │  State C    │   │  State A    │
        │  (▼ ▼▼)     │   │  (▶ ▶▶)     │
        │  All levels │   │  Nothing    │
        │  visible    │   │  visible    │
        └─────────────┘   └─────────────┘
                 │             │
    Click ▼▼     │             │      Click ▶▶
   (fold all)    │             │      (unfold all)
                 ▼             ▼
        ┌─────────────┐   ┌─────────────┐
        │  State B    │   │  State C    │
        │  (▼ ▶▶)     │   │  (▼ ▼▼)     │
        │  First      │   │  All levels │
        │  level      │   │  visible    │
        │  visible    │   │             │
        └─────────────┘   └─────────────┘
                 │
    Click ▼      │
   (fold 1)      │
                 ▼
        ┌─────────────┐
        │  State A    │
        │  (▶ ▶▶)     │
        │  Nothing    │
        │  visible    │
        └─────────────┘
```

## Simplified Transition Table

| Current State | Click Button 1 | Result | Click Button 2 | Result |
|---------------|----------------|--------|----------------|--------|
| **A: ▶ ▶▶** (nothing) | ▶ (unfold 1) | **B: ▼ ▶▶** (first level) | ▶▶ (unfold all) | **C: ▼ ▼▼** (all levels) |
| **B: ▼ ▶▶** (first level) | ▼ (fold 1) | **A: ▶ ▶▶** (nothing) | ▶▶ (unfold all) | **C: ▼ ▼▼** (all levels) |
| **C: ▼ ▼▼** (all levels) | ▼ (fold 1) | **A: ▶ ▶▶** (nothing) | ▼▼ (fold all) | **B: ▼ ▶▶** (first level) |

## Key Insights

1. **Button 1 (fold/unfold one level)**:
   - From State A (▶): Unfolds to first level → State B (▼)
   - From State B or C (▼): Folds completely → State A (▶)
   - **Always toggles between "nothing" and "first level"**

2. **Button 2 (fold/unfold all levels)**:
   - From State A (▶▶): Unfolds to all levels → State C (▼▼)
   - From State B (▶▶): Unfolds to all levels → State C (▼▼)
   - From State C (▼▼): Folds to first level (NOT nothing) → State B (▼ ▶▶)
   - **When unfolding (▶▶), always shows ALL levels. When folding (▼▼), goes back to first level only.**

3. **Coordination**:
   - When button 1 changes, button 2 updates accordingly
   - When button 2 changes, button 1 updates accordingly
   - The impossible state "▶ ▼▼" is prevented by design

## Initial State

- **Sessions and User messages**: Start in **State B** (▼ ▶▶) - first level visible
- **Assistant, System, Thinking, Tools**: Start in **State A** (▶ ▶▶) - fully folded

## Example Flow

**Starting from State A (fully folded):**

1. User sees: `▶ 2 messages    ▶▶ 125 total`
2. Clicks ▶▶ (unfold all) → Goes to State C, sees everything
3. Now sees: `▼ fold 2    ▼▼ fold all below`
4. Clicks ▼▼ (fold all) → Goes back to State B, sees only first level
5. Now sees: `▼ fold 2    ▶▶ fold all 125 below`
6. Clicks ▼ (fold one) → Goes to State A, sees nothing
7. Back to: `▶ 2 messages    ▶▶ 125 total`
8. Clicks ▶ (unfold one) → Goes to State B, sees first level
9. Now sees: `▼ fold 2    ▶▶ fold all 125 below`

This creates a natural exploration pattern: nothing → all levels → first level → nothing → first level.
