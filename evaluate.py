import os
import time
import anthropic
from anthropic import APIStatusError
from dotenv import load_dotenv
from db import init_db, already_seen, save_company, get_company_by_id, update_company_report
import json

load_dotenv()
client = anthropic.Anthropic()

EXTRACTION_PROMPT = """You are reading a startup/tech newsletter. Extract any startups mentioned.

Rules:
- Only include startups and early-stage companies, not large established companies (e.g. Google, Apple)
- Do not include publications, newsletters, or individual people
- Use the company's canonical/official name
- If a company is mentioned multiple times, include it only once
- If no startups are found, return an empty array"""

REPORT_PROMPT = """You are a startup analyst helping a senior embedded/firmware engineer evaluate companies to join.
Write a thoughtful research report on this company. Be specific where you can, and honest about uncertainty where you can't.
Keep each answer to 2-3 sentences maximum. Be direct and avoid filler phrases.

Company: {name}
Description: {description}

Research (web search results):
{research_context}

Use the research to populate factual fields (location, company size, funding stage, recent news). Cite specific facts from the research where possible. For fields where the research provides no data, reason from first principles and explicitly note the uncertainty.

Evaluate the company on each dimension below."""

DEALBREAKER_ORDER = [
    "developing_hardware",
    "is_startup",
]

DEALBREAKER_SPECS = {
    "developing_hardware": {
        "question": "Is this company developing physical hardware — sensors, devices, robotics, or physical systems? Chip design and fabless semiconductor companies do NOT qualify.",
        "search_guidance": "Search for what physical products they build, hardware demos, product pages, or firmware/embedded job postings. Confirm or rule out chip design/fabless if unclear.",
    },
    "is_startup": {
        "question": "Is this a startup — not a large established company? Look for founding year (ideally post-2015), headcount (ideally under ~500), and funding stage (ideally pre-Series C).",
        "search_guidance": "Search for founding year, total headcount (LinkedIn, Crunchbase), and latest funding round stage.",
    },
    "billion_dollar_potential": {
        "question": "Does this company have billion-dollar potential? Focus on: (1) total addressable market size — is the problem space worth billions?, (2) strategic differentiation — does the company have a defensible moat, proprietary technology, or network effects?, (3) real traction — named customers, deployments, contracts, or partnerships that validate the market. Do NOT use funding size as a negative signal — early-stage hardware companies routinely raise small seed rounds and still reach billion-dollar outcomes. A $3-5M seed is completely normal. Only flag funding as a concern if the company has been around many years with no institutional backing at all.",
        "search_guidance": "Search for market size estimates for the problem they solve, any named customers or deployments, and what makes their approach defensible or unique.",
    },
    "growing_quickly": {
        "question": "Is this company growing quickly? Look for recent funding rounds (last 18 months), headcount growth, new customer wins, contracts, product launches, or high-frequency recent press.",
        "search_guidance": "Search for the most recent funding round date/amount, headcount on LinkedIn, and any news from the last 12–18 months.",
    },
    "solves_real_problem": {
        "question": "Does this company solve a real, significant pain point with clear customer demand? Look for named customers, contracts, pilots, or explicit customer validation.",
        "search_guidance": "Search for named customer wins, pilot programs, government contracts, partnerships, or direct customer quotes.",
    },
}

DISCOVERY_PROMPT = """Research {name}.{context_hint}

Use up to TWO web searches. Build a clear picture of:
- What exactly they build — be specific about the physical product, technology, or system (not marketing language)
- The core problem they're solving and who the customer is
- What makes their approach novel or differentiated from alternatives
- Founding year, funding stage and total raised, approximate headcount
- Any notable traction: customers, contracts, deployments, or partnerships

If the company name is ambiguous, use your first search to confirm you're looking at the hardware/deep-tech startup, not an unrelated business with the same name.

Return a factual summary of 300–400 words. Write in plain prose — no markdown headers, no bullet points, no formatting. Lead with a concrete description of the product — this is the most important part."""

PREFILTER_PROMPT = """You are doing a quick pre-screening of a startup to decide whether it might be a hardware company worth researching further.

Company: {name}
Description: {description}

Question: Could this company be developing physical hardware (sensors, devices, robots, vehicles, industrial equipment, medical devices, consumer electronics, or any other physical product)?

Return "no" ONLY if the description makes it unmistakably obvious this is a pure software/services company with no physical product — for example: "online payments platform", "HR software", "legal automation tool", "social media app", "SaaS analytics dashboard".

If there is ANY doubt — biotech, medtech, defense, energy, manufacturing, or anything that could involve a physical component — return "unknown".

When in doubt, always return "unknown". A false pass costs a few cents. A false reject loses a good company."""

DEALBREAKER_MODELS = {
    "developing_hardware": "claude-sonnet-4-6",
    "is_startup":          "claude-haiku-4-5-20251001",
}

DEALBREAKER_EVAL_PROMPT = """You are evaluating a startup for a senior embedded/firmware engineer considering companies to join.

Company: {name}
Newsletter description: {description}
Discovery summary: {discovery_context}

The newsletter description is how the company was characterized in a tech/startup newsletter — useful framing, but may be promotional. Prioritize the discovery summary and any web search results for factual assessments; use the newsletter description as supporting context.

Your task: answer one specific question about this company.

Question: {question}

Search guidance: {search_guidance}

You have up to 2 web searches. Search only if the discovery summary leaves genuine uncertainty about this specific question. If it already answers it clearly, call submit_dealbreaker_answer directly.

When ready, call submit_dealbreaker_answer."""

REPORT_RESEARCH_PROMPT = """Research {name} for a detailed evaluation report.

Already known: {discovery_context}

You have up to 3 web searches. Focus on what the summary above doesn't cover:
- Exact headcount or size signals
- Technical depth: what's novel about their approach, specific engineering problems
- Defensibility, competition, market timing
- Total funding raised
- Recent momentum (last 12 months): contracts, product news, hires

Be specific. Cite sources where possible."""

FULL_REPORT_REQUIRED_FIELDS = {
    "location",
    "mission",
    "company_size",
    "billion_dollar_potential",
    "growing_quickly",
    "solves_real_problem",
    "monopoly_potential",
    "novelty",
    "breakthrough_vs_incremental",
    "timing",
    "unique_opportunity",
    "learning_opportunities",
    "transferable_skills",
}


class CreditExhaustedError(Exception):
    """Raised when the API returns a billing/credit error (non-retryable)."""
    pass


_usage = {"input_tokens": 0, "output_tokens": 0}

def _reset_usage():
    _usage["input_tokens"] = 0
    _usage["output_tokens"] = 0

def _current_cost() -> float:
    i, o = _usage["input_tokens"], _usage["output_tokens"]
    return i / 1_000_000 * 3.00 + o / 1_000_000 * 15.00

def _format_cost():
    i, o = _usage["input_tokens"], _usage["output_tokens"]
    return f"  Cost: ~${_current_cost():.4f}  ({i:,} in / {o:,} out tokens)"


def call_with_retry(fn, retries=4, delay=10):
    """Retry an API call with exponential backoff on overload errors."""
    for attempt in range(retries):
        try:
            result = fn()
            if hasattr(result, 'usage'):
                _usage["input_tokens"] += getattr(result.usage, 'input_tokens', 0)
                _usage["output_tokens"] += getattr(result.usage, 'output_tokens', 0)
            return result
        except APIStatusError as e:
            if e.status_code == 402:
                raise CreditExhaustedError(f"API credit exhausted (HTTP 402): {e.message}") from e
            if e.status_code != 529:
                raise
            if attempt == retries - 1:
                raise
            wait = delay * (2 ** attempt)  # 10s, 20s, 40s, 80s
            print(f"  API overloaded, retrying in {wait}s... (attempt {attempt + 1}/{retries})")
            time.sleep(wait)


def _agentic_search_loop(messages, tools, max_tokens, model="claude-sonnet-4-6"):
    """Run the agentic web search loop, accumulating text output. Returns (accumulated_text, final_response)."""
    accumulated = []
    response = None
    while True:
        response = call_with_retry(lambda: client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            tools=tools,
        ))
        last_search_idx = max(
            (i for i, b in enumerate(response.content)
             if getattr(b, 'type', '') in ('web_search_tool_result', 'server_tool_use')),
            default=-1
        )
        if last_search_idx >= 0:
            post = [b.text for b in response.content[last_search_idx + 1:]
                    if hasattr(b, 'text') and b.text and b.text.strip()]
            accumulated.extend(post)
        else:
            cont = [b.text for b in response.content
                    if hasattr(b, 'text') and b.text and b.text.strip()]
            accumulated.extend(cont)

        if response.stop_reason in ("pause_turn", "max_tokens"):
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
            continue
        break

    return "\n".join(accumulated), response


def extract_companies(newsletter_text: str) -> list:
    response = call_with_retry(lambda: client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[
            {"role": "user", "content": EXTRACTION_PROMPT + "\n\nNewsletter:\n" + newsletter_text}
        ],
        tools=[{
            "name": "save_startups",
            "description": "Save the list of startups extracted from the newsletter",
            "input_schema": {
                "type": "object",
                "properties": {
                    "startups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Official company name"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "One sentence describing what the company does and what problem it solves"
                                }
                            },
                            "required": ["name", "description"]
                        }
                    }
                },
                "required": ["startups"]
            }
        }],
        tool_choice={"type": "tool", "name": "save_startups"}
    ))

    return response.content[0].input.get("startups", [])


def discover_company(name: str, description_hint: str = "") -> str:
    context_hint = f" Context: {description_hint}" if description_hint else ""
    prompt = DISCOVERY_PROMPT.format(name=name, context_hint=context_hint)
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]
    try:
        result, _ = _agentic_search_loop(messages, tools, max_tokens=2048)
        print(f"  Discovery complete: {len(result)} chars")
        return result
    except Exception as e:
        print(f"  Discovery failed ({type(e).__name__}): {e}")
        return ""


def prefilter_hardware(name: str, description: str) -> str:
    """Cheap Haiku call (no web search) to reject obviously non-hardware companies.
    Returns 'no' or 'unknown' only — never 'yes'."""
    prompt = PREFILTER_PROMPT.format(name=name, description=description)
    try:
        response = call_with_retry(lambda: client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "name": "submit_prefilter",
                "description": "Submit your pre-filter answer.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string", "enum": ["no", "unknown"]}
                    },
                    "required": ["answer"]
                }
            }],
            tool_choice={"type": "tool", "name": "submit_prefilter"},
        ))
        return response.content[0].input.get("answer", "unknown")
    except Exception as e:
        print(f"  Pre-filter failed ({type(e).__name__}): {e}")
        return "unknown"


def evaluate_dealbreaker(name: str, description: str, discovery_context: str, dealbreaker_key: str) -> dict:
    spec = DEALBREAKER_SPECS[dealbreaker_key]
    prompt = DEALBREAKER_EVAL_PROMPT.format(
        name=name,
        description=description or "No newsletter description available.",
        discovery_context=discovery_context or "No discovery data available.",
        question=spec["question"],
        search_guidance=spec["search_guidance"],
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
        {
            "name": "submit_dealbreaker_answer",
            "description": "Submit your structured answer for this dealbreaker.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "enum": ["yes", "no", "unknown"]},
                    "reason": {"type": "string", "description": "1-2 sentences citing specific facts."}
                },
                "required": ["answer", "reason"]
            }
        }
    ]
    model = DEALBREAKER_MODELS.get(dealbreaker_key, "claude-sonnet-4-6")
    try:
        while True:
            response = call_with_retry(lambda: client.messages.create(
                model=model,
                max_tokens=1024,
                messages=messages,
                tools=tools,
            ))
            if response.stop_reason == "tool_use":
                for block in response.content:
                    if getattr(block, 'type', '') == 'tool_use' and block.name == "submit_dealbreaker_answer":
                        return block.input

            if response.stop_reason in ("pause_turn", "max_tokens"):
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
                continue

            # end_turn without structured answer
            break

        return {"answer": "unknown", "reason": "Model did not submit a structured answer."}
    except Exception as e:
        print(f"  Dealbreaker {dealbreaker_key} failed ({type(e).__name__}): {e}")
        return {"answer": "unknown", "reason": f"Evaluation error: {e}"}


def check_dealbreakers_sequential(name: str, description: str, discovery_context: str) -> tuple[bool, dict]:
    results = {}

    if description:
        prefilter = prefilter_hardware(name, description)
        if prefilter == "no":
            print(f"  [{name}] Pre-filter: unmistakably non-hardware. Skipping.")
            results = {k: {"answer": "unknown", "reason": "Not evaluated (pre-filter rejected as non-hardware)."} for k in DEALBREAKER_ORDER}
            results["developing_hardware"] = {"answer": "no", "reason": "Description-only pre-filter: unmistakably non-hardware."}
            return False, results

    for key in DEALBREAKER_ORDER:
        print(f"  [{name}] Evaluating {key}...")
        result = evaluate_dealbreaker(name, description, discovery_context, key)
        results[key] = result
        fail = result["answer"] == "no" or (key in ("developing_hardware", "is_startup") and result["answer"] == "unknown")
        if fail:
            if result["answer"] == "unknown":
                print(f"  [{name}] Could not confirm hardware — treating as fail. Aborting.")
            else:
                print(f"  [{name}] Failed {key}: {result['reason']}. Aborting.")
            for remaining in DEALBREAKER_ORDER:
                if remaining not in results:
                    results[remaining] = {"answer": "unknown", "reason": "Not evaluated (earlier dealbreaker failed)."}
            return False, results
    passed = all(v["answer"] != "no" for v in results.values())
    return passed, results


def research_for_report(name: str, discovery_context: str) -> str:
    prompt = REPORT_RESEARCH_PROMPT.format(name=name, discovery_context=discovery_context or "No discovery data available.")
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    try:
        result, _ = _agentic_search_loop(messages, tools, max_tokens=2048)
        print(f"  Report research complete: {len(result)} chars")
        return result
    except Exception as e:
        print(f"  Report research failed ({type(e).__name__}): {e}")
        return ""


def generate_report(name: str, description: str, dealbreaker_results: dict, research_context: str = "") -> dict:
    prompt = REPORT_PROMPT.format(name=name, description=description, research_context=research_context or "No research available.")
    response = call_with_retry(lambda: client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "name": "save_report",
            "description": "Save the startup research report",
            "input_schema": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "object",
                        "description": "The company's headquarters city and country. If unknown, say 'Unknown'.",
                        "properties": {
                            "answer": {"type": "string"}
                        },
                        "required": ["answer"]
                    },
                    "mission": {
                        "type": "object",
                        "description": "The company's core mission and what they're trying to achieve in the world, in 1-2 sentences.",
                        "properties": {
                            "answer": {"type": "string"}
                        },
                        "required": ["answer"]
                    },
                    "company_size": {
                        "type": "object",
                        "description": "What is the size of the company? 30-100 people is ideal for a senior hire with meaningful equity and impact.",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "billion_dollar_potential": {
                        "type": "object",
                        "description": "Does this company have billion-dollar potential? Consider total addressable market size, strategic defensibility (proprietary tech, network effects, moat), and real traction (named customers, contracts, deployments). Do not treat small seed funding as a negative signal.",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "growing_quickly": {
                        "type": "object",
                        "description": "Is this company growing quickly? Look for recent funding rounds (last 18 months), headcount growth, new customer wins, product launches, or high-frequency press coverage.",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "solves_real_problem": {
                        "type": "object",
                        "description": "Does this company solve a real, significant pain point with clear customer demand? Look for named customers, contracts, pilots, partnerships, or direct validation.",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "monopoly_potential": {
                        "type": "object",
                        "description": "Does this have the potential to be a monopoly in its space?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "novelty": {
                        "type": "object",
                        "description": "Is the company building something wholly new, or combining things in a novel way?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "breakthrough_vs_incremental": {
                        "type": "object",
                        "description": "Is this a breakthrough technology or an incremental improvement on existing solutions?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "timing": {
                        "type": "object",
                        "description": "Is this the right time to be building this company? Why now?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "unique_opportunity": {
                        "type": "object",
                        "description": "Is this company taking advantage of a unique opportunity that others don't see?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "learning_opportunities": {
                        "type": "object",
                        "description": "What are the most interesting technical or domain problems an engineer would work on here? What unique learning could you get that would be hard to find at most other companies?",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    },
                    "transferable_skills": {
                        "type": "object",
                        "description": "What technical skills gained here would remain valuable outside this company and into the future? Focus on skills that are durable or increasingly important given the rise of AI — e.g. rare hardware expertise, systems-level thinking, deep domain knowledge in a growing field, skills that complement rather than compete with AI.",
                        "properties": {
                            "answer": {"type": "string"},
                            "assessment": {"type": "string", "enum": ["good", "neutral", "bad"]}
                        },
                        "required": ["answer", "assessment"]
                    }
                },
                "required": ["location", "mission", "company_size", "billion_dollar_potential", "growing_quickly", "solves_real_problem", "monopoly_potential", "novelty", "breakthrough_vs_incremental", "timing", "unique_opportunity", "learning_opportunities", "transferable_skills"]
            }
        }],
        tool_choice={"type": "tool", "name": "save_report"}
    ))
    report = response.content[0].input
    report["dealbreakers"] = dealbreaker_results
    return report


def has_full_report(report: dict) -> bool:
    return FULL_REPORT_REQUIRED_FIELDS.issubset(report.keys())


def generate_full_report_for_company_id(company_id: int) -> tuple[dict, bool]:
    """Generate or reuse a full report for a saved company.

    Returns (result, generated), where result contains the company row fields and
    report_json. Raises ValueError for user-facing validation failures.
    """
    init_db()
    row = get_company_by_id(company_id)
    if not row:
        raise ValueError(f"No company found with id {company_id}.")

    _, name, first_seen, source, passed_dealbreakers, report_json, _ = row
    if not passed_dealbreakers:
        raise ValueError(f"{name} did not pass dealbreakers.")
    if not report_json:
        raise ValueError(f"{name} has no dealbreaker report saved.")

    report = json.loads(report_json)
    if has_full_report(report):
        return {
            "id": company_id,
            "name": name,
            "first_seen": first_seen,
            "source": source,
            "report_json": report_json,
        }, False

    description = report.get("_description") or name
    discovery_context = report.get("_discovery_context") or ""
    dealbreaker_results = report.get("dealbreakers")
    if not dealbreaker_results:
        raise ValueError(f"{name} has no dealbreaker results saved.")

    if not discovery_context:
        print(f"  [{name}] Missing discovery context, discovering before report...")
        discovery_context = discover_company(name, description)
        report["_discovery_context"] = discovery_context

    print(f"  [{name}] Researching for full report...")
    report_context = research_for_report(name, discovery_context)

    print(f"  [{name}] Generating full report...")
    full_report = generate_report(name, description, dealbreaker_results, report_context)
    full_report["_description"] = description
    full_report["_discovery_context"] = discovery_context
    update_company_report(company_id, json.dumps(full_report))

    return {
        "id": company_id,
        "name": name,
        "first_seen": first_seen,
        "source": source,
        "report_json": json.dumps(full_report),
    }, True


# Backward-compatible wrappers for scout_bot.py and rerun.py
def research_company(name: str, description_hint: str = "") -> str:
    return discover_company(name, description_hint)


def check_dealbreakers(name: str, description: str, research_context: str = "") -> tuple[bool, dict]:
    return check_dealbreakers_sequential(name, description, research_context)


def process_newsletter(text: str, source: str = "manual"):
    init_db()
    print(f"\nExtracting companies from newsletter...")
    companies = extract_companies(text)
    print(f"Found {len(companies)} companies: {[c['name'] for c in companies]}")

    for company in companies:
        try:
            name = company["name"]
            description = company["description"]

            if already_seen(name):
                print(f"  [{name}] Already in database, skipping.")
                continue

            _reset_usage()
            print(f"  [{name}] Discovering...")
            discovery_context = discover_company(name, description)
            if not discovery_context:
                print(f"  [{name}] Warning: discovery returned nothing, evaluating without web data.")

            print(f"  [{name}] Checking dealbreakers...")
            passed, dealbreaker_results = check_dealbreakers_sequential(name, description, discovery_context)

            if not passed:
                failed = [k for k, v in dealbreaker_results.items() if v["answer"] == "no"]
                print(f"  [{name}] Failed dealbreakers: {failed}. Skipping.")
                save_company(name, source, passed=False, cost=_current_cost())
                continue

            report = {
                "_description": description,
                "_discovery_context": discovery_context,
                "dealbreakers": dealbreaker_results,
            }
            save_company(name, source, passed=True, report=json.dumps(report), cost=_current_cost())
            print(f"  [{name}] Passed dealbreakers. Saved for weekly report.")
        except Exception as e:
            print(f"  [{company.get('name', 'unknown')}] ERROR: {e}")
            continue


def evaluate_company(name: str):
    """Evaluate a single company by name. Prints research, dealbreakers, and report."""
    _reset_usage()
    init_db()
    print(f"\n=== Discovering {name} ===")
    discovery_context = discover_company(name)
    if discovery_context:
        print(f"\n--- Discovery Brief ({len(discovery_context)} chars) ---")
        print(discovery_context)
    else:
        print("WARNING: Discovery returned nothing.")

    description = discovery_context.split("\n\n")[0].strip() if discovery_context else name

    print(f"\n=== Dealbreakers ===")
    passed, dealbreaker_results = check_dealbreakers_sequential(name, description, discovery_context)
    for key, value in dealbreaker_results.items():
        icon = "✅" if value["answer"] == "yes" else ("⚠️ " if value["answer"] == "unknown" else "❌")
        print(f"{icon} {key}: {value['reason']}")

    if not passed:
        print(f"\nDid not pass dealbreakers.")
        print(_format_cost())
        return

    print(f"\n=== Researching for report ===")
    report_context = research_for_report(name, discovery_context)

    print(f"\n=== Report ===")
    report = generate_report(name, description, dealbreaker_results, report_context)
    location = report.get("location", {}).get("answer", "Unknown")
    mission = report.get("mission", {}).get("answer", "")
    print(f"Location: {location}")
    if mission:
        print(f"Mission: {mission}")
    for key, value in report.items():
        if not isinstance(value, dict) or "answer" not in value or key in ("location", "mission"):
            continue
        assessment = value.get("assessment", "").upper()
        print(f"\n[{assessment}] {key.replace('_', ' ').title()}")
        print(value["answer"])

    print(f"\n{_format_cost()}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--company":
        # Usage: python evaluate.py --company "Ulysses"
        evaluate_company(" ".join(sys.argv[2:]))
    elif len(sys.argv) > 1:
        # Usage: python evaluate.py newsletter.txt
        with open(sys.argv[1], "r") as f:
            text = f.read()
        process_newsletter(text, source=sys.argv[1])
    else:
        # Paste mode
        print("Paste newsletter text below. When done, enter a new line with just END:")
        lines = []
        while True:
            line = input()
            if line.strip() == "END":
                break
            lines.append(line)
        process_newsletter("\n".join(lines), source="manual")
