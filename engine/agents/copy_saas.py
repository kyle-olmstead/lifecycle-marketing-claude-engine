"""Offline starter copy for software: the welcome series' roles, which engine/agents/copy_library.py writes for a store
(best sellers, how ordering works, a first-order nudge), written about the first step in the product instead.

copywriter.offline_drafts lays these over the library when the brief's business model is software (saas, subscription,
services, sales_led_b2b) and the flow is a welcome series. Same contract as the other libraries: two structurally
different variants per role, a framework and a falsifiable hypothesis each, no [brackets], no offer and no deadline.
"""
from __future__ import annotations

from typing import Any, Dict, List

from engine.models import Brief, base_form

P = "{product_name}"


def applies(brief: Brief) -> bool:
    from engine.flows.library import _family
    return brief.flow_type == "welcome" and _family(brief.business_model) != "ecommerce"


def role_label(role: str, brief: Brief) -> str:
    """A role's name in words; a software welcome series nudges toward a first step, not a first order."""
    if role == "first_order_nudge" and applies(brief):
        return "first step nudge"
    return role.replace("_", " ")


def first_step(brief: Brief) -> str:
    """The first step in words a customer reads: the core action, unless it is the generic activation event, which reads as
    jargon ("reach activation") in an email."""
    core = brief.core_conversion_event
    if core.event == "activation_reached" and not core.where:
        return "get set up"
    return base_form(core.label or core.event.replace("_", " "))


def email_library(brief: Brief) -> Dict[str, List[Dict[str, Any]]]:
    do = first_step(brief)
    return {
        "best_sellers": [
            {"subject": "Where most people start", "preheader": "One step, a few minutes",
             "headline": "Start where most people do", "paragraphs": [
                 f"Hi {{first_name}}, if you are not sure where to begin in {P}, start where most new users do: {do}.",
                 "It takes a few minutes, and everything after it builds on it. If you want a hand, reply to this email."],
             "cta_label": "Take the first step", "framework": "Curated first step",
             "hypothesis": "Naming the one step most people take first raises the goal rate within the outcome window because it removes "
                           "the choice; falsified if B converts as well or better."},
            {"subject": "Not sure where to start?", "preheader": "The question new users ask most",
             "headline": "Where to begin", "paragraphs": [
                 f"Hi {{first_name}}, the question we hear most from new users is where to start in {P}.",
                 f"Our answer: {do} first, with your own work rather than an example. Reply if you get stuck and a person will help."],
             "cta_label": "Show me where", "framework": "Question-led guidance",
             "hypothesis": "Leading with the reader's own question raises clicks more than a direct instruction; falsified if A's click "
                           "rate is equal or higher."},
        ],
        "social_proof": [
            {"subject": f"What people who stay with {P} do first", "preheader": "In their first week",
             "headline": "What the first week looks like", "paragraphs": [
                 f"Hi {{first_name}}, the people who get the most from {P} tend to {do} in their first week, usually in one sitting.",
                 "If you have not yet, the button below takes you straight there."],
             "cta_label": "Do it now", "framework": "Social proof through a common pattern",
             "hypothesis": "Describing what retained users do in week one raises the goal rate because it is easy to imitate; falsified "
                           "if B converts as well or better."},
            {"subject": "The step that makes it click", "preheader": "What new users tell us",
             "headline": "The step that makes it click", "paragraphs": [
                 f"Hi {{first_name}}, new users tell us {P} made sense the moment they did one thing: {do}.",
                 "It takes a few minutes. Reply if anything gets in the way."],
             "cta_label": "Try it", "framework": "Social proof through other users' words",
             "hypothesis": "Framing the step as the moment it made sense raises the goal rate more than a pattern; falsified if A converts "
                           "as well or better."},
        ],
        "how_it_works": [
            {"subject": f"How {P} works, in three steps", "preheader": "What to do first, and where to get help",
             "headline": "Three things to know", "paragraphs": [
                 f"Hi {{first_name}}, first: {do}. It takes a few minutes and everything else builds on it.",
                 "Second: your work is saved, so come back whenever you need it. Third: if anything is unclear, reply to any email and a "
                 "person will help."],
             "cta_label": "Get started", "framework": "Risk reversal through clarity",
             "hypothesis": "Answering what to do first and where help is before it is asked raises the goal rate; falsified if B converts "
                           "as well or better."},
            {"subject": "The questions new users ask us", "preheader": "Where to start, and what if you get stuck",
             "headline": "Your questions, answered", "paragraphs": [
                 "Hi {first_name}, in the first week most people want to know two things: where to start, and who to ask when they get "
                 "stuck.",
                 f"Start here: {do}. And when you get stuck, reply to any of these emails: a person reads them."],
             "cta_label": "Start here", "framework": "FAQ as reassurance",
             "hypothesis": "Framing the email as answers raises clicks among undecided new users; falsified if A's click rate is equal or "
                           "higher."},
        ],
        "first_order_nudge": [
            {"subject": "Still getting started?", "preheader": "No rush. Here is the first step",
             "headline": "Whenever you are ready", "paragraphs": [
                 f"Hi {{first_name}}, no pressure from us. When you are ready, the button below takes you straight to the first step: {do}.",
                 "Questions first? Reply and a person will answer."],
             "cta_label": "Take the first step", "framework": "Plain reminder",
             "hypothesis": "A low-pressure reminder converts some undecided new users without raising unsubscribes; falsified if this "
                           "send's unsubscribe rate exceeds the flow average."},
            {"subject": "A quick question, {first_name}", "preheader": "What is in the way?",
             "headline": "What would help?", "paragraphs": [
                 "Hi {first_name}, you signed up a little while ago and have not had a chance to get going yet. That is completely fine.",
                 "If something is in the way, a question, a missing feature, time, reply with one line and we will answer it."],
             "cta_label": f"Open {P}", "framework": "Question-led objection discovery",
             "hypothesis": "Inviting a reply surfaces what stops new users and converts some readers; falsified if B's goal rate is equal "
                           "to or below A's and replies are under 1%."},
        ],
    }


def short_library(brief: Brief) -> Dict[str, Dict[str, List[Any]]]:
    do = first_step(brief)
    Do = do[:1].upper() + do[1:]
    return {
        "best_sellers": {
            "sms": [f"Not sure where to start in {P}? Most people {do} first. {{link}}", f"Where to begin in {P}: {do}, a few minutes. {{link}}"],
            "push": [("Where most people start", f"{Do} first; it takes a few minutes."), ("Not sure where to start?", f"{Do} first.")],
            "in_app": [("Where most people start", f"{Do} first; everything else builds on it.", "Start"),
                       ("Not sure where to start?", f"Most new users {do} first.", "Show me")]},
        "social_proof": {
            "sms": [f"People who stay with {P} {do} in their first week. {{link}}", f"New users say {P} clicked once they did this: {do}. {{link}}"],
            "push": [("What the first week looks like", f"Most people {do} in week one."), ("The step that makes it click", f"{Do}.")],
            "in_app": [("What the first week looks like", f"People who stay with {P} {do} in their first week.", "Do it now"),
                       ("The step that makes it click", f"New users say it made sense once they did this: {do}.", "Try it")]},
        "how_it_works": {
            "sms": [f"{P} in one line: {do} first, and reply any time you get stuck. {{link}}", f"Stuck in {P}? Reply here and a person will help."],
            "push": [("How it works", f"{Do} first; your work is saved."), ("Stuck?", "Reply to our email and a person will help.")],
            "in_app": [("How it works", f"{Do} first. Your work is saved, and a person answers every reply.", "Get started"),
                       ("Your questions, answered", f"Start with this: {do}.", "Start here")]},
        "first_order_nudge": {
            "sms": [f"Whenever you are ready, {P} starts here: {do}. {{link}}", f"Anything in the way with {P}? Reply with one line."],
            "push": [("Whenever you are ready", f"The first step: {do}."), ("What would help?", "Reply to our email with one line.")],
            "in_app": [("Whenever you are ready", f"The first step is to {do}.", "Take the first step"),
                       ("What would help?", "Tell us in one line what is in the way.", "Tell us")]},
    }
