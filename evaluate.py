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

DEALBREAKER_PROMPT = """You are evaluating a startup to see if it's worth deeper research for a senior embedded/firmware engineer looking to join a hardware startup.

Company: {name}
Description: {description}

Research (web search results):
{research_context}

Use the research to inform your answers. If the research contradicts the description, trust the research. If the research is empty or unhelpful for a criterion, fall back to the description.

Evaluate the company against each of the following criteria and provide a boolean answer and a one-line reason for each."""

REPORT_PROMPT = """You are a startup analyst helping a senior embedded/firmware engineer evaluate companies to join.
Write a thoughtful research report on this company. Be specific where you can, and honest about uncertainty where you can't.
Keep each answer to 2-3 sentences maximum. Be direct and avoid filler phrases.

Company: {name}
Description: {description}

Research (web search results):
{research_context}

Use the research to populate factual fields (location, company size, funding stage, recent news). Cite specific facts from the research where possible. For fields where the research provides no data, reason from first principles and explicitly note the uncertainty.

Evaluate the company on each dimension below."""


def call_with_retry(fn, retries=4, delay=10):
    """Retry an API call with exponential backoff on overload errors."""
    for attempt in range(retries):
        try:
            return fn()
        except APIStatusError as e:
            if e.status_code != 529:
                raise
            if attempt == retries - 1:
                raise
            wait = delay * (2 ** attempt)  # 10s, 20s, 40s, 80s
            print(f"  API overloaded, retrying in {wait}s... (attempt {attempt + 1}/{retries})")
            time.sleep(wait)


def extract_companies(newsletter_text: str) -> list:
    response = call_with_retry(lambda: client.messages.create(
        model="claude-opus-4-6",
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


def research_company(name: str, description_hint: str = "") -> str:
    prompt = (
        f"Research the startup '{name}'."
        + (f" Context: {description_hint}\n\n" if description_hint else "\n\n")
        + "Return a structured brief covering:\n"
        "- What they build and the problem they solve\n"
        "- Company location and approximate headcount\n"
        "- Funding stage and notable investors\n"
        "- Recent news or growth signals\n"
        "- What kind of firmware/embedded/hardware engineering work they do (check job listings)\n"
        "Be concise and factual. If you can't find information on a point, say so."
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    try:
        while True:
            response = call_with_retry(lambda: client.messages.create(
                model="claude-opus-4-6",
                max_tokens=1024,
                messages=messages,
                tools=tools,
            ))
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
                continue
            break
        return next((b.text for b in response.content if hasattr(b, "text")), "")
    except Exception as e:
        print(f"  Research failed ({type(e).__name__}): {e}")
        return ""


def check_dealbreakers(name: str, description: str, research_context: str = "") -> tuple[bool, dict]:
    prompt = DEALBREAKER_PROMPT.format(name=name, description=description, research_context=research_context or "No research available.")
    response = call_with_retry(lambda: client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "name": "save_dealbreaker_results",
            "description": "Save the dealbreaker evaluation results",
            "input_schema": {
                "type": "object",
                "properties": {
                    "developing_hardware": {
                        "type": "object",
                        "description": "Is the company developing hardware (physical products, sensors, devices, robotics, etc.)? Chip design / fabless semiconductor companies do NOT qualify — this role requires hands-on embedded/firmware work, not RTL or silicon design.",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "is_startup": {
                        "type": "object",
                        "description": "Is this a startup (not a large established company)?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "solves_real_problem": {
                        "type": "object",
                        "description": "Does the company solve a real, significant pain point?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "growing_quickly": {
                        "type": "object",
                        "description": "Is the company growing quickly?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    },
                    "billion_dollar_potential": {
                        "type": "object",
                        "description": "Does this company have the potential to be a billion-dollar company?",
                        "properties": {
                            "answer": {"type": "boolean"},
                            "reason": {"type": "string"}
                        },
                        "required": ["answer", "reason"]
                    }
                },
                "required": ["developing_hardware", "is_startup", "solves_real_problem", "growing_quickly", "billion_dollar_potential"]
            }
        }],
        tool_choice={"type": "tool", "name": "save_dealbreaker_results"}
    ))
    result = response.content[0].input
    passed = all(v["answer"] for v in result.values())
    return passed, result


def generate_report(name: str, description: str, dealbreaker_results: dict, research_context: str = "") -> dict:
    prompt = REPORT_PROMPT.format(name=name, description=description, research_context=research_context or "No research available.")
    response = call_with_retry(lambda: client.messages.create(
        model="claude-opus-4-6",
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

            print(f"  [{name}] Researching...")
            research_context = research_company(name, description)

            print(f"  [{name}] Checking dealbreakers...")
            passed, dealbreaker_results = check_dealbreakers(name, description, research_context)

            if not passed:
                failed = [k for k, v in dealbreaker_results.items() if not v["answer"]]
                print(f"  [{name}] Failed dealbreakers: {failed}. Skipping.")
                save_company(name, source, passed=False)
                continue

            print(f"  [{name}] Passed! Generating report...")
            report = generate_report(name, description, dealbreaker_results, research_context)
            report["_description"] = description
            save_company(name, source, passed=True, report=json.dumps(report))
            print(f"  [{name}] Done.")
        except Exception as e:
            print(f"  [{company.get('name', 'unknown')}] ERROR: {e}")
            continue


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
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