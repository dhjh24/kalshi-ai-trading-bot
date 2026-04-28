#!/usr/bin/env python3

import asyncio
import sys
sys.path.append('.')

from src.clients.model_router import ModelRouter

async def extract_analysis():
    """Extract a free-form trading analysis via the configured LLM provider."""

    router = ModelRouter()

    analysis_prompt = """
You are an expert quantitative trading analyst. Based on our Kalshi trading system state:

**CURRENT PORTFOLIO:**
- 16 active positions with high capital utilization (~91%)
- $9.84 available cash remaining
- Mix of automated and manual trades
- Current positions are unrealized P&L

**KEY CONTEXT:**
- Earlier manual trades were mostly profitable
- Recent positions haven't closed yet so appear as 0% win rate
- System has evolved from manual to automated trading
- Need to distinguish between manual success and automated performance

**ANALYSIS REQUEST:**
Provide actionable performance insights and improvement recommendations for this Kalshi prediction market trading system. Focus on:

1. Capital management optimization
2. Position sizing strategy
3. Market selection criteria
4. Risk management improvements
5. Lessons from profitable manual trading to apply to automation

Be specific and actionable.
"""

    try:
        response_content = await router.get_completion(
            prompt=analysis_prompt,
            max_tokens=3000,
            temperature=0.3,
            strategy="extract_grok_analysis",
            query_type="completion",
        )

        if response_content is None:
            print("LLM provider returned no response.")
            return

        cost = router.get_total_cost()

        print("GROK4 TRADING SYSTEM ANALYSIS")
        print("=" * 60)
        print(response_content)
        print("=" * 60)
        print(f"Analysis cost: ${cost:.4f}")

        # Save to file
        with open('grok4_full_analysis.txt', 'w') as f:
            f.write("GROK4 Trading Analysis\n")
            f.write("=" * 60 + "\n\n")
            f.write(response_content)
            f.write(f"\n\n[Analysis cost: ${cost:.4f}]")

        print("\nFull analysis saved to: grok4_full_analysis.txt")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await router.close()

if __name__ == "__main__":
    asyncio.run(extract_analysis())
