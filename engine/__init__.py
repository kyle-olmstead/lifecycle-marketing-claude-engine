"""The LMM Engine: deterministic code for everything in lifecycle marketing that must never be guessed.

Claude writes language; this package computes. A one-sentence brief becomes a build-ready plan through
a pipeline of typed stages (engine.pipeline), each an agent with one job (engine.agents). Policies a
program depends on (sending gate, frequency, offers, consent, send windows) are typed contracts that
code evaluates (engine.policy). Deliverability, measurement, rendering and SQL each have their own
package. Source and destination adapters (moving data in from Shopify, Stripe, GA4 and the rest, and
building flows in each ESP) live beside this package in adapters/.

Every job runs through one command, `lcm` (engine.cli), offline, with no API keys unless a step
explicitly sends something and the user approved it.
"""

__version__ = "3.0.0"
