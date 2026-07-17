# Startup Scout Evaluation Playbook

This document describes the complete evaluation procedure for the Startup Scout pipeline. An AI instance can follow these steps to evaluate a company (or batch of companies from a newsletter) and produce the same output as the automated pipeline — without making any API calls.

---

## Evaluator Context

You are helping a **senior embedded/firmware engineer** (Canadian, non-US citizen) evaluate hardware startups as potential employers. The target company profile is:

- Developing **physical hardware** — sensors, devices, robotics, physical systems
- **Early-stage startup** — founded post-2015, under ~500 employees, pre-Series C funding
- 30–100 people ideal (meaningful equity and impact for a senior hire)
- Novel technology, strong market timing, real customer traction
- Located anywhere, but must not require US citizenship for engineering roles

You are **not** evaluating as an investor. You are asking: *would a senior firmware/embedded engineer want to work here, and would it be a career-defining move?*

---

## Pipeline Overview

```
Newsletter text
    │
    ▼
Stage 1: Extract company names and descriptions
    │
    ▼ (for each company)
Stage 2: Check if already seen → skip if yes
    │
    ▼
Stage 3: Pre-filter — obviously non-hardware? → skip if yes
    │
    ▼
Stage 4: Discovery research (web search, 300–400 word summary)
    │
    ▼
Stage 5: Dealbreaker 1 — Developing hardware?  → skip if no/unknown
    │
    ▼
Stage 6: Dealbreaker 2 — Is a startup?         → skip if no/unknown
    │
    ▼
Stage 7: Category exclusions (ITAR, quantum, medical) → skip if excluded
    │
    ▼
Stage 8: SAVE TO DB — company passes, queued for weekly report
    │
    ▼ (triggered separately via Telegram)
Stage 9: Deep research + full report generation
```

---

## Stage 1: Newsletter Extraction

Read the newsletter and extract a list of startups mentioned. For each, note:
- **name** — canonical/official company name
- **description** — one sentence: what they do and what problem they solve

**Rules:**
- Only startups and early-stage companies. Exclude large established companies (Google, Apple, Airbus, etc.)
- Exclude publications, newsletters, funds, and individual people
- If a company appears multiple times, include it once
- If no startups are found, the newsletter yields an empty list

---

## Stage 2: Deduplication

Before evaluating a company, check whether it already exists in the database (case-insensitive name match). If it does, skip it entirely — no further evaluation needed.

---

## Stage 3: Pre-filter (Description-Only)

Using only the extracted one-sentence description (no web search), ask:

> Could this company be developing physical hardware (sensors, devices, robots, vehicles, industrial equipment, consumer electronics, or any other physical product)?

**Return "no" only** if the description makes it unmistakably obvious this is a pure software/services company — for example: "online payments platform", "HR software", "legal automation tool", "social media app", "SaaS analytics dashboard".

**Return "unknown"** (and continue) if there is ANY doubt — biotech, medtech, defense, energy, manufacturing, or anything that could involve a physical component.

**Bias strongly toward "unknown".** A false pass costs evaluation time. A false reject permanently loses a good company.

If the pre-filter returns "no": skip the company, record as failed on `developing_hardware`.

---

## Stage 4: Discovery Research

Search the web to build a factual 300–400 word plain-prose summary of the company. Use up to two searches. If the company name is ambiguous, use the first search to confirm you have the right entity (the hardware/deep-tech startup, not an unrelated business with the same name).

The summary must cover:
1. **What they build** — be specific about the physical product, technology, or system. No marketing language. What does the hardware actually do?
2. **The problem and customer** — who suffers the problem they solve, and how they suffer it
3. **Differentiation** — what makes their approach novel vs. existing alternatives
4. **Founding year, funding stage, total raised, approximate headcount**
5. **Traction** — named customers, contracts, deployments, partnerships, or pilots

Write in plain prose. No markdown headers, no bullet points. Lead with the product description — this is the most important part.

---

## Stage 5: Dealbreaker 1 — Developing Hardware

**Question:** Is this company developing physical hardware — sensors, devices, robotics, or physical systems?

**Chip design and fabless semiconductor companies do NOT qualify.** If the company designs chips (ASICs, FPGAs, SoCs) but does not build the physical systems that use them, this is a fail.

**Search guidance:** Look for physical product pages, hardware demos, firmware/embedded job postings. Confirm or rule out chip design/fabless if unclear.

**Answer options:**
- `yes` — clear physical hardware product
- `no` — definitely software-only, services, chip design, or fabless
- `unknown` — insufficient evidence to confirm

**Outcome:**
- `yes` → continue to Stage 6
- `no` → skip company, record failure
- `unknown` → **treat as failure** (same as "no"). We cannot place a senior engineer at a company we cannot confirm builds hardware.

**Examples that pass:** Access control readers, autonomous drones, bioacoustic edge devices, nuclear reactors, space launch vehicles, robotic systems, industrial sensors, BLE mesh hardware.

**Examples that fail:** Cloud APIs, SaaS platforms, AI model companies, chip design startups (fabless), software-only robotics platforms that use third-party hardware.

---

## Stage 6: Dealbreaker 2 — Is a Startup

**Question:** Is this a startup — not a large established company?

Look for:
- **Founding year** — ideally post-2015
- **Headcount** — ideally under ~500 employees
- **Funding stage** — ideally pre-Series C

**Answer options:**
- `yes` — clearly an early-stage startup on at least two of the three criteria
- `no` — clearly a mature/large company
- `unknown` — insufficient evidence

**Outcome:**
- `yes` → continue to Stage 7
- `no` → skip company, record failure
- `unknown` → **treat as failure**

**Notes:**
- A company can be post-2015 but already large — that's a fail on headcount/stage
- A company can have a large total raise but still be pre-Series C — use stage, not absolute dollars
- If a company is clearly a unicorn ($1B+ valuation, hundreds of employees), it fails even if the funding stage is technically Series B

---

## Stage 7: Category Exclusions

After passing both dealbreakers, check three exclusion categories. These are binary: if any applies, the company is excluded.

### 7a. ITAR / Citizenship Requirement

> Does this company's core engineering work require US citizenship or a security clearance?

**Exclude if:** The company builds military rockets/missiles, weapons systems, military satellite payloads, or is involved in classified defense programs that legally require US persons.

**Do NOT exclude for:** Commercial space, general robotics, defense software, surveillance hardware, dual-use technology, commercial nuclear energy, or any company where citizenship is not clearly a legal requirement for engineers.

**When uncertain, do not exclude.** Only exclude if clearly applicable.

### 7b. Quantum Computing

> Is this primarily a quantum computing company?

**Exclude if:** The core business is quantum processors, quantum algorithms, quantum networking, or quantum sensing as the primary product.

**Do NOT exclude for:** Companies that use "quantum-inspired" classical algorithms, or companies in adjacent fields that are not primarily quantum.

### 7c. Medical Device

> Is this primarily a medical device company?

**Exclude if:** The core product is an FDA-regulated implant, diagnostic hardware, surgical robot, or wearable health monitor.

**Do NOT exclude for:** Health software, drug discovery, biotech without a physical device, or general-purpose hardware that happens to have medical applications.

---

## Stage 8: Company Passes

If a company clears all stages, it is saved to the database and queued for the weekly report email. At this stage, only the discovery summary and dealbreaker results are saved — no further evaluation is run automatically.

The weekly report will show the discovery summary and a link to generate the full report via Telegram.

---

## Stage 9: Full Report (On-Demand Only)

The full report is only generated when explicitly requested via Telegram (`/full <id>` or the email deep link). It is never generated automatically during newsletter processing.

To generate a full report, perform additional web research (up to 3 more searches) covering:
- Exact headcount or size signals
- Technical depth: what is novel about their engineering approach, specific problems being solved
- Defensibility, competition, market timing
- Total funding raised (all rounds)
- Recent momentum (last 12 months): contracts, product news, hires

Then evaluate the company on all 13 report dimensions:

### Report Dimensions

Each dimension gets:
- **answer** — 2–3 sentences, specific, no filler
- **assessment** — `good`, `neutral`, or `bad`

Be direct. Cite specific facts. If the research doesn't cover a field, reason from first principles and explicitly flag the uncertainty.

---

#### Location
Headquarters city and country. If unknown, say so.
*(No assessment — factual only)*

---

#### Mission
The company's core mission in 1–2 sentences: what are they trying to achieve in the world?
*(No assessment — factual only)*

---

#### Company Size
What is the headcount? Is it in the ideal 30–100 range for a senior hire with meaningful equity and impact?

| Assessment | Criteria |
|---|---|
| `good` | 30–100 employees — right size for equity and impact |
| `neutral` | 10–30 (very early, thin team) or 100–300 (still small enough) |
| `bad` | Under 10 (pre-product) or over 300 (equity diluted, less impact) |

---

#### Billion Dollar Potential
Does this company have the potential to become a billion-dollar company?

Consider: (1) TAM — is the problem space worth billions? (2) Defensibility — proprietary tech, network effects, moat? (3) Traction — named customers, contracts, deployments validating the market.

**Do not penalize small seed rounds** — early-stage hardware companies routinely raise $3–5M seed rounds and still reach billion-dollar outcomes. Only flag funding as a concern if the company has been around many years with no institutional backing.

| Assessment | Criteria |
|---|---|
| `good` | Large TAM + defensible position + real traction |
| `neutral` | Large TAM but weak moat, or strong moat but small/uncertain market |
| `bad` | Small TAM, no differentiation, or no customer validation |

---

#### Growing Quickly
Is the company showing strong growth signals?

Look for: recent funding rounds (last 18 months), headcount growth, new customer wins, product launches, high-frequency press coverage.

| Assessment | Criteria |
|---|---|
| `good` | Recent funding + clear headcount/customer growth in the last 12–18 months |
| `neutral` | Some signals but no clear acceleration |
| `bad` | No recent news, stagnant headcount, no new customers |

---

#### Solves Real Problem
Does the company solve a real, significant pain point with demonstrated customer demand?

Look for: named customers, contracts, pilots, partnerships, or direct customer quotes.

| Assessment | Criteria |
|---|---|
| `good` | Multiple named customers, contracts, or pilots in production |
| `neutral` | Some validation but limited or early-stage |
| `bad` | No named customers, purely theoretical demand |

---

#### Monopoly Potential
Does this company have the potential to dominate its space?

Consider: proprietary technology lock-in, data flywheels, network effects, switching costs, standards ownership.

| Assessment | Criteria |
|---|---|
| `good` | Clear path to winner-takes-most via defensible moat |
| `neutral` | Competitive differentiation but no structural moat |
| `bad` | Commodity approach, easily replicated, fragmented market |

---

#### Novelty
Is the company building something wholly new, or combining existing things in a genuinely novel way?

| Assessment | Criteria |
|---|---|
| `good` | Genuinely new product category or approach with no clear prior art |
| `neutral` | Meaningful improvement on existing solutions |
| `bad` | Incremental feature differentiation on a known product |

---

#### Breakthrough vs Incremental
Is this a breakthrough technology or an incremental improvement?

| Assessment | Criteria |
|---|---|
| `good` | Fundamental change in how a problem is solved — new physics, new architecture |
| `neutral` | Meaningful but evolutionary improvement |
| `bad` | Better/cheaper version of an existing product with no structural advantage |

---

#### Timing
Is this the right moment to be building this company? What has changed recently that makes this viable now?

Consider: enabling technology that recently matured, regulatory changes, market structural shifts, geopolitical tailwinds.

| Assessment | Criteria |
|---|---|
| `good` | Clear "why now" — specific recent enabler makes this the right moment |
| `neutral` | Reasonable timing but no strong tailwind |
| `bad` | Either too early (enabling tech not ready) or too late (incumbents entrenched) |

---

#### Unique Opportunity
Is this company taking advantage of a window that others don't see or can't easily access?

Consider: proprietary research origin, founder domain expertise, geographic access, early recognition of an emerging standard.

| Assessment | Criteria |
|---|---|
| `good` | Clear asymmetric advantage — insight, access, or timing others don't have |
| `neutral` | Good opportunity but not uniquely positioned to capture it |
| `bad` | Many competitors see the same opportunity with equal or better position |

---

#### Learning Opportunities
What are the most interesting technical and domain problems an engineer would work on here?

Focus on: what unique technical skills or knowledge would be developed that would be hard to find at most other companies? Be specific about the engineering problems.

| Assessment | Criteria |
|---|---|
| `good` | Rare, deep technical problems — systems-level, cross-disciplinary, not replicable elsewhere |
| `neutral` | Interesting work but not uniquely stretching |
| `bad` | Commodity engineering — execution of known solutions |

---

#### Transferable Skills
What skills gained here remain valuable outside this company and into the future?

Focus on skills that are **durable** and **increasingly important** as AI becomes more prevalent — rare hardware expertise, systems-level thinking, deep domain knowledge in a growing field, skills that complement rather than compete with AI.

| Assessment | Criteria |
|---|---|
| `good` | Rare, AI-resistant skills with growing demand (e.g. BLE/RF, embedded security, robotics, edge AI hardware) |
| `neutral` | Solid skills but not rare or particularly durable |
| `bad` | Skills that are commoditizing or likely to be displaced |

---

## Assessment Summary

After scoring all 13 dimensions, mentally tally the assessments:

- **8+ good**: Strong candidate — worth a Telegram full-report request
- **5–7 good**: Solid but with meaningful gaps — review the `bad` fields carefully
- **Under 5 good**: Weak fit even if it passed dealbreakers

The most heavily weighted dimensions (in order of importance to the evaluator):
1. Learning Opportunities
2. Novelty + Breakthrough vs Incremental
3. Transferable Skills
4. Timing + Unique Opportunity
5. Company Size (30–100 ideal)

---

## Quick Reference: Common Failure Patterns

| Pattern | Fails at |
|---|---|
| "AI platform for X" with no hardware | Pre-filter or `developing_hardware` |
| Chip/ASIC design company | `developing_hardware` |
| Software that runs on third-party hardware | `developing_hardware` |
| Founded pre-2010, 1000+ employees | `is_startup` |
| Series D or later | `is_startup` |
| Military rockets, missile systems | Category exclusion (ITAR) |
| Quantum processor / quantum algorithm company | Category exclusion (quantum) |
| FDA-regulated implant or diagnostic device | Category exclusion (medical device) |
| Commercial nuclear / energy hardware | Passes all stages |
| Defense hardware with non-classified customers | Passes all stages |
| Space hardware for commercial applications | Passes all stages |
