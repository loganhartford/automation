import os
import time
import anthropic
from anthropic import APIStatusError
from dotenv import load_dotenv
from db import init_db, already_seen, save_company
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
    "billion_dollar_potential",
    "growing_quickly",
    "solves_real_problem",
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
        "question": "Does this company have billion-dollar potential? Consider total addressable market size, strategic differentiation, and total funding raised (large raises signal investor conviction).",
        "search_guidance": "Search for TAM estimates, total funding raised and investors, and analyst commentary on the space.",
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

Use ONE web search. Find: what they actually build (specific product/technology), founding year, funding stage, approximate size, and the problem they solve.

If the company name is ambiguous, search with context to find the hardware/deep-tech startup, not an unrelated business with the same name.

Return a concise factual summary (150–250 words)."""

DEALBREAKER_EVAL_PROMPT = """You are evaluating a startup for a senior embedded/firmware engineer considering companies to join.

Company: {name}
Discovery summary: {discovery_context}

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
        model="claude-sonnet-4-6",
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
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
    try:
        result, _ = _agentic_search_loop(messages, tools, max_tokens=1024)
        print(f"  Discovery complete: {len(result)} chars")
        return result
    except Exception as e:
        print(f"  Discovery failed ({type(e).__name__}): {e}")
        return ""


def evaluate_dealbreaker(name: str, discovery_context: str, dealbreaker_key: str) -> dict:
    spec = DEALBREAKER_SPECS[dealbreaker_key]
    prompt = DEALBREAKER_EVAL_PROMPT.format(
        name=name,
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
    try:
        while True:
            response = call_with_retry(lambda: client.messages.create(
                model="claude-sonnet-4-6",
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
    for key in DEALBREAKER_ORDER:
        print(f"  [{name}] Evaluating {key}...")
        result = evaluate_dealbreaker(name, discovery_context, key)
        results[key] = result
        if result["answer"] == "no":
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
                "required": ["location", "mission", "company_size", "monopoly_potential", "novelty", "breakthrough_vs_incremental", "timing", "unique_opportunity", "learning_opportunities", "transferable_skills"]
            }
        }],
        tool_choice={"type": "tool", "name": "save_report"}
    ))
    report = response.content[0].input
    report["dealbreakers"] = dealbreaker_results
    return report


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

            print(f"  [{name}] Discovering...")
            discovery_context = discover_company(name, description)
            if not discovery_context:
                print(f"  [{name}] Warning: discovery returned nothing, evaluating without web data.")

            print(f"  [{name}] Checking dealbreakers...")
            passed, dealbreaker_results = check_dealbreakers_sequential(name, description, discovery_context)

            if not passed:
                failed = [k for k, v in dealbreaker_results.items() if v["answer"] == "no"]
                print(f"  [{name}] Failed dealbreakers: {failed}. Skipping.")
                save_company(name, source, passed=False)
                continue

            print(f"  [{name}] Passed! Researching for report...")
            report_context = research_for_report(name, discovery_context)

            print(f"  [{name}] Generating report...")
            report = generate_report(name, description, dealbreaker_results, report_context)
            report["_description"] = description
            save_company(name, source, passed=True, report=json.dumps(report))
            print(f"  [{name}] Done.")
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
