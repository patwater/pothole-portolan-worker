# Chicago: working notes

Behavior: Scout, 2026-11-24. Nothing here is a claim. Every statement below is a hypothesis drawn from background knowledge, not from a fetched page, and no search or fetch was made in this run. No figure below may be used until Fetch archives its source and Extract writes a claim.

## Data regime (appears to be)

**A large, record-level 311 demand feed on the city's open data portal, with no obvious network-wide pavement condition index.** This is the demand-heavy regime that the pilot agenda proposed for Chicago. The condition side is the part most likely to be missing, so Discover must look for it and report plainly if it is not found.

| Strand | What appears to exist | Kind when extracted | Lead (unverified) |
|--------|-----------------------|---------------------|-------------------|
| Demand | City 311 Service Requests on the Chicago Data Portal (Socrata), record-level and refreshed frequently, with pothole-related request types | `reported_demand` | Dataset id `v6vf-nfxy`, on data.cityofchicago.org |
| Demand, historical | A separate "Pot Holes Reported" dataset that appears to cover an earlier period and may have been superseded by the 311 feed | `reported_demand` | Dataset id `7as2-ds3y`, on data.cityofchicago.org |
| Condition | Unknown. A pavement condition survey or a rating of arterial and local streets may exist in Department of Transportation reports, budget documents or an inspector general audit, but none has been seen | `measured_condition` | Not found yet |
| Response and program output | Possibly pothole repair counts or crew-day figures in the Streets and Sanitation or Transportation budget books and performance reports | `program_output` or `response_time` | Not found yet |
| Spending | Resurfacing and street-repair appropriations in the annual budget, if separable from other transport spending | `spending` | Not found yet |

## Things to watch for in later behaviors

- **Dataset ids are not yet confirmed.** Both ids above come from background knowledge. Discover must confirm them on the portal, check the date coverage of each, and say whether one replaced the other. Fetch must not build URLs from an id until the portal page confirms it. A wrong id should fail safe, because `scripts/fetch_source.py` refuses any Chicago dataset that is not registered there.
- **Aggregate-only fetch.** The 311 feed is record-level and has address, location and free-text fields. Rule 7 applies, and the fetch guard for these two ids allows only grouped counts. Record-level rows must never be archived or committed.
- **Which request types count as a pothole.** The 311 feed uses request-type and sub-type labels, and the definition of a pothole request has to be read from the dataset's own documentation and the observed values, not assumed. The two relevant labels may have changed over time.
- **Demand is not condition.** A 311 count measures how much residents report, which depends on app use, trust and demographics. It must stay `reported_demand`, and it must never be set beside a condition figure as if it measured road condition. Chicago counts must not be used to rank the city against others.
- **Duplicates and open cases.** Requests may be duplicated, and open cases may still change status. Any caveat the city gives about this belongs in the claim's `caveats` field.
- **Possible condition sources to check.** A city pavement condition report, a capital improvement plan with a condition summary, a Chicago Inspector General or City Auditor document on street repair, and the annual budget books are the likely places to look. Each must be read for the index name, scale, survey year, share of network surveyed and method before any claim is written. Condition indices from other cities or countries must not be compared with any Chicago figure unless a published crosswalk is cited.
- **Response times.** If the city reports a repair time or service-level target, the claim must state whether it is a target or a measured result.

## Open questions for Discover and Fetch

1. Are dataset ids `v6vf-nfxy` and `7as2-ds3y` current on the portal, and what date range does each cover?
2. Does the 311 feed's pothole category stay stable over the years, and did the historical dataset stop being updated?
3. Does Chicago publish any pavement condition index, survey or pavement management report, and if so, what is its method and survey share?
4. Does the city publish pothole repair counts or crew output as a program measure, and in which document?
5. Is street-repair spending separable in the budget, so a `spending` claim can be stated without mixing it with other transport costs?

## Scout result (2026-11-24)

No sources listed, none fetched, no claims written. `claims.yaml` and `sources.yaml` are empty lists, as the content checks require from this stage. The next behavior is Discover.

## Extract result (2026-11-24)

No claims written. The archived copies of the two 311 datasets are metadata only (column lists), and no archived source holds a count, so any number would have come from memory. Two grouped-count queries were listed in `sources.yaml` instead: `chi-311-pothole-by-year-status` (current feed, pothole-type rows by year, status, duplicate and legacy flags) and `chi-311-pothole-historical-by-year` (historical dataset, by year, status and request type, plus the recorded potholes-filled sum). Both pass the aggregate-only policy. The stage goes back to discovered so Fetch archives them. Still unread: the CDOT service page text (a stated target, if any) and the BGA snapshot; the official 2026 budget book URL is still needed. OIG audits remain unavailable (robots 403).
