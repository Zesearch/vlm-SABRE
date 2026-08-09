# Texture Recipe

Texture tests whether a model recognizes a counterfactual material from visual
evidence rather than answering with the familiar object's canonical material.
The Base image shows the normal surface, and the Edited image changes only that
surface to the counterfactual material.

Canonical Q1-Q4 probes:

- Base normal surface: yes
- Base counterfactual surface: no
- Edited normal surface: no
- Edited counterfactual surface: yes

The primary metric is strict pair accuracy: all four probes must be correct for
the pair to count as correct.
