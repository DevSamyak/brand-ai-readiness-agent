# 🎼 Orchestrator (`run_audit.py`) - The Audit Entrypoint

Welcome to the command center of our **Brand AI-Readiness Marketplace**. 

This script (`run_audit.py`) is the designated **entrypoint** for the entire audit. Instead of forcing an AI agent (or a human) to run five different scripts manually, this orchestrator manages the entire workflow from a single command. It is completely read-only, incredibly fast, and handles all the data merging automatically.

## 🗺️ How It Works (The Workflow)

To respect the hackathon's performance and composition rules, the orchestrator ensures we only touch the live website **once**. Here is how it flows:

```mermaid
graph TD
    A([User / AI Agent]) -->|Provides URL| B[🕷️ crawl.py]
    B -->|Fetches Raw HTML| C[(Offline JSON Cache)]
    C -->|Reads Data| D[🧠 check_crawlability.py <br/> <i>(Discoverability)</i>]
    C -->|Reads Data| E[🔎 extract_facts.py <br/> <i>(Freshness)</i>]
    C -->|Reads Data| F[🧲 check_engagement.py <br/> <i>(Engagement)</i>]
    D --> G{Merge & Format}
    E --> G
    F --> G
    G -->|Generates for Agents| H[📄 report.json]
    G -->|Generates for Humans| I[📝 report.md]
    
    style A fill:#ff9900,stroke:#333,stroke-width:2px
    style C fill:#bbf,stroke:#333,stroke-width:2px
    style G fill:#ffcc00,stroke:#333,stroke-width:2px
    style H fill:#bfb,stroke:#333,stroke-width:2px
    style I fill:#bfb,stroke:#333,stroke-width:2px
```

### The 4-Step Process

1. **The Single Crawl:** It first runs `crawl.py` to visit the target URL, respects `robots.txt`, and saves everything (HTML, headers, links) into a temporary offline cache.
2. **The Offline Audit:** It passes this cache to our three specialized auditor skills (Crawlability, Freshness, and Engagement). Since they read offline data, they run near-instantly.
3. **The Assembly:** It collects all the findings, sorts them by severity (Critical down to Low), and standardizes the ID numbers (e.g., `F-001`, `F-002`).
4. **The Output:** It generates a strict `report.json` (perfect for an AI agent to parse) and an optional Markdown file (perfect for a human to read).

## 🚀 How to Run It

You can trigger the entire marketplace audit using this single command.

**Basic Run:**
```bash
python run_audit.py --url https://www.example.com
```
*(This will generate a `report.json` file in your current directory).*

**Advanced Run (Human Readable + Deeper Crawl):**
If you want to crawl more pages and get a pretty Markdown report alongside the JSON:
```bash
python run_audit.py --url https://www.example.com --max-pages 10 --md-out report.md
```

## 🏆 Why This Design Wins
* **Separation of Concerns:** Each auditor script minds its own business. The orchestrator is the only script that knows how to merge them.
* **Agent-Friendly Limitations:** The orchestrator clearly injects a `limitations` block into the JSON report, explicitly telling the AI agent what it needs to manually double-check (like running a live Google search to verify volatile facts like pricing).
* **Crash-Proof:** If one sub-skill crashes or finds nothing, the orchestrator catches it, prints a warning, and still generates the final report using the remaining successful skills.