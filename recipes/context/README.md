# Context Recipe

Context tests whether a model follows visual evidence when an unexpected entity
appears in a familiar scene. The Base image contains the context-consistent
source object, and the Edited image replaces it with a visually plausible but
context-unexpected target object.

Canonical Q1-Q4 probes:

- Base source: yes
- Base target: no
- Edited source: no
- Edited target: yes

The primary metric is strict pair accuracy: all four probes must be correct for
the pair to count as correct.
