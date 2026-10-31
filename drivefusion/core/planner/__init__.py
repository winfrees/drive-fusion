"""The consolidation planner: where to add copies (PLAN §9, ROADMAP M7).

It only ever adds placements. Every existing copy is pinned, so no plan can
imply a deletion; a shortfall becomes an ``acquire`` step. Plans are
documents a person carries out; nothing here touches a drive.
"""
