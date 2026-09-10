"""Cypher query library for ownership traversal.

These are the analytical payoff of resolution: each query is unanswerable
without it, because every one of them depends on following a chain across the
point where the same party appears under two different filings.

Percentage propagation
----------------------
Companies House publishes ownership as bands, not point estimates. Multiplying
band midpoints down a chain would produce a single confident-looking number
that the source does not support. These queries multiply the *bounds* instead,
so a two-hop chain of "25-50%" through "50-75%" yields 12.5-37.5% — an honest
interval. Where a hop asserts control without a percentage (right to appoint
directors, significant influence), percentage arithmetic is abandoned entirely
and the path is flagged as control-bearing instead, because multiplying an
unknown by anything is not a number.

Depth limits
------------
Every traversal is bounded. Corporate ownership graphs contain genuine cycles —
mutual holdings, circular structures used deliberately to frustrate tracing —
and an unbounded variable-length match on a graph this size does not terminate
usefully. Six hops covers the overwhelming majority of real structures; the
cycle query exists precisely to find the ones that do not terminate.
"""

from __future__ import annotations

__all__ = [
    "BENEFICIAL_OWNERS",
    "CIRCULAR_OWNERSHIP",
    "CROSS_BORDER_CHAINS",
    "HUB_ENTITIES",
    "OPAQUE_STRUCTURES",
    "OWNERSHIP_CHAIN",
    "QUERIES",
    "SANCTIONED_PATH_TO_COMPANY",
    "SANCTIONS_EXPOSURE",
    "sanctions_exposure_query",
]


BENEFICIAL_OWNERS = """
// Ultimate beneficial owners of a company: natural persons reachable by
// following control edges upward, with no further owner above them.
MATCH path = (owner:Person)-[:CONTROLS*1..6]->(target:Company)
WHERE target.reg_number = $reg_number
  AND NOT EXISTS { MATCH (:Entity)-[:CONTROLS]->(owner) }
WITH owner, path,
     reduce(lo = 1.0, r IN relationships(path) |
            lo * coalesce(r.min_percent, 0.0) / 100.0) AS min_share,
     reduce(hi = 1.0, r IN relationships(path) |
            hi * coalesce(r.max_percent, 100.0) / 100.0) AS max_share,
     any(r IN relationships(path) WHERE r.min_percent IS NULL)  AS control_only,
     any(r IN relationships(path) WHERE r.via_fiduciary)        AS via_fiduciary,
     length(path) AS hops
RETURN owner.canonical_id  AS owner_id,
       owner.name          AS owner_name,
       owner.nationality   AS nationality,
       owner.is_sanctioned AS is_sanctioned,
       owner.is_pep        AS is_pep,
       hops,
       CASE WHEN control_only THEN NULL ELSE round(min_share * 100, 2) END AS min_percent,
       CASE WHEN control_only THEN NULL ELSE round(max_share * 100, 2) END AS max_percent,
       control_only,
       via_fiduciary
ORDER BY coalesce(max_percent, 999) DESC, hops ASC
"""

OWNERSHIP_CHAIN = """
// Full control path between two named entities, in either direction.
MATCH path = (a:Entity {canonical_id: $from_id})-[:CONTROLS*1..8]->(b:Entity {canonical_id: $to_id})
RETURN [n IN nodes(path) | {id: n.canonical_id, name: n.name,
                            type: n.entity_type, jurisdiction: n.jurisdiction}] AS chain,
       [r IN relationships(path) | {min: r.min_percent, max: r.max_percent,
                                    kinds: r.control_kinds}] AS steps,
       length(path) AS hops
ORDER BY hops ASC
LIMIT 25
"""

CIRCULAR_OWNERSHIP = """
// Circular control structures: an entity that ultimately controls itself.
// Legitimate in a handful of group structures, and a deliberate obfuscation
// technique in others — either way, these break naive upward traversal, so
// finding them is a prerequisite for trusting any UBO result.
MATCH path = (e:Entity)-[:CONTROLS*2..6]->(e)
WITH e, path, length(path) AS cycle_length
RETURN e.canonical_id AS entity_id,
       e.name         AS entity_name,
       cycle_length,
       [n IN nodes(path) | n.name] AS cycle
ORDER BY cycle_length ASC
LIMIT $limit
"""

# Note on `{max_hops}`: Cypher does not accept a *parameter* as a
# variable-length path bound — `[:CONTROLS*1..$max_hops]` is a syntax error,
# because the planner needs the bound at compile time. It is therefore a Python
# format placeholder, substituted by `sanctions_exposure_query()` below, which
# coerces it to an int first. Everything else stays a real query parameter;
# only the path bound is interpolated.
SANCTIONS_EXPOSURE = """
// Every UK company controlled, directly or indirectly, by a sanctioned entity.
// This is the query the whole pipeline exists to make answerable: the link is
// invisible at one hop, because the sanctioned party rarely appears on the
// target company's own filing.
MATCH path = (risk:Entity)-[:CONTROLS*1..{max_hops}]->(company:Company)
WHERE risk.is_sanctioned = true
WITH company, risk, path, length(path) AS hops,
     reduce(hi = 1.0, r IN relationships(path) |
            hi * coalesce(r.max_percent, 100.0) / 100.0) AS max_share,
     any(r IN relationships(path) WHERE r.min_percent IS NULL) AS control_only
RETURN company.canonical_id AS company_id,
       company.name         AS company_name,
       company.reg_number   AS company_number,
       collect(DISTINCT {name: risk.name, id: risk.canonical_id,
                         hops: hops, topics: risk.risk_topics})[0..5] AS sanctioned_owners,
       min(hops)            AS shortest_hops,
       max(CASE WHEN control_only THEN NULL ELSE round(max_share * 100, 2) END)
                            AS max_indirect_percent
ORDER BY shortest_hops ASC, company_name
LIMIT $limit
"""

SANCTIONED_PATH_TO_COMPANY = """
// The specific chain connecting a sanctioned party to one company — the
// evidence an analyst needs before acting on a hit.
MATCH path = (risk:Entity {canonical_id: $risk_id})-[:CONTROLS*1..6]->
             (company:Company {reg_number: $reg_number})
RETURN [n IN nodes(path) | {name: n.name, type: n.entity_type,
                            jurisdiction: n.jurisdiction,
                            sanctioned: n.is_sanctioned}] AS chain,
       [r IN relationships(path) | {min: r.min_percent, max: r.max_percent,
                                    kinds: r.control_kinds,
                                    fiduciary: r.via_fiduciary}] AS steps,
       length(path) AS hops
ORDER BY hops ASC
LIMIT 10
"""

OPAQUE_STRUCTURES = """
// Companies whose control chain passes through a secrecy jurisdiction, or
// through a fiduciary layer, and therefore cannot be resolved to a natural
// person from open data. Not evidence of wrongdoing — evidence that open data
// runs out, which is the measurable quantity that matters for coverage.
MATCH path = (top:Entity)-[:CONTROLS*1..6]->(company:Company)
WHERE NOT EXISTS { MATCH (:Entity)-[:CONTROLS]->(top) }
  AND (top.jurisdiction IN $secrecy_jurisdictions
       OR any(r IN relationships(path) WHERE r.via_fiduciary))
  AND top.entity_type <> 'Person'
RETURN company.canonical_id AS company_id,
       company.name         AS company_name,
       company.reg_number   AS company_number,
       top.name             AS terminal_entity,
       top.jurisdiction     AS terminal_jurisdiction,
       length(path)         AS hops,
       any(r IN relationships(path) WHERE r.via_fiduciary) AS via_fiduciary
ORDER BY hops DESC
LIMIT $limit
"""

HUB_ENTITIES = """
// Entities controlling an unusually large number of companies. A mix of
// genuine holding groups, nominee directors, and — where resolution has gone
// wrong — over-merged clusters. Reviewing the top of this list is the cheapest
// available check on cluster quality at full scale, where no labels exist.
MATCH (e:Entity)-[:CONTROLS]->(c:Company)
WITH e, count(DISTINCT c) AS controlled
WHERE controlled >= $min_controlled
RETURN e.canonical_id AS entity_id,
       e.name         AS entity_name,
       e.entity_type  AS entity_type,
       e.n_records    AS source_records,
       controlled
ORDER BY controlled DESC
LIMIT $limit
"""

CROSS_BORDER_CHAINS = """
// Control chains crossing at least one jurisdiction boundary, counted by
// terminal jurisdiction. The distribution is the headline coverage finding:
// where does control over UK companies actually terminate?
MATCH path = (top:Entity)-[:CONTROLS*1..6]->(company:Company)
WHERE NOT EXISTS { MATCH (:Entity)-[:CONTROLS]->(top) }
  AND top.jurisdiction <> '' AND top.jurisdiction <> 'GB'
RETURN top.jurisdiction AS terminal_jurisdiction,
       count(DISTINCT company) AS companies_controlled,
       count(DISTINCT top)     AS controlling_entities,
       round(avg(length(path)), 2) AS mean_hops
ORDER BY companies_controlled DESC
"""


def sanctions_exposure_query(max_hops: int = 6) -> str:
    """Return the sanctions-exposure query with its path bound substituted.

    ``max_hops`` is coerced to an int and clamped, so the interpolation cannot
    carry anything but a small integer into the query text.

    A targeted ``str.replace`` rather than ``str.format``: Cypher map literals
    (``{name: risk.name, ...}``) are themselves braces, and ``format`` reads
    them as replacement fields and raises. Only the single named placeholder is
    substituted.
    """
    bound = max(1, min(int(max_hops), 10))
    return SANCTIONS_EXPOSURE.replace("{max_hops}", str(bound))


QUERIES: dict[str, str] = {
    "beneficial_owners": BENEFICIAL_OWNERS,
    "ownership_chain": OWNERSHIP_CHAIN,
    "circular_ownership": CIRCULAR_OWNERSHIP,
    "sanctions_exposure": sanctions_exposure_query(),
    "sanctioned_path": SANCTIONED_PATH_TO_COMPANY,
    "opaque_structures": OPAQUE_STRUCTURES,
    "hub_entities": HUB_ENTITIES,
    "cross_border_chains": CROSS_BORDER_CHAINS,
}
