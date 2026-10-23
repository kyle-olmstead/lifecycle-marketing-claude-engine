"""Offline starter copy for stores: the roles a store shares with software (win-back, social proof, last nudge, the
re-permission ask), written about orders and products instead of workspaces and setup.

copywriter.offline_drafts lays these over the software library when the brief's business model is a store's (ecommerce
or marketplace). Same contract as engine/agents/copy_library.py: two structurally different variants per role, a
framework and a falsifiable hypothesis each, no [brackets], no offer and no deadline. Item-aware roles (social proof in
a cart or browse flow) show the items with the {product_block} paragraph and name the first one with {item_name}.
"""
from __future__ import annotations

from typing import Any, Dict, List

from engine.models import Brief

P = "{product_name}"


def is_store(brief: Brief) -> bool:
    from engine.flows.library import _family
    return _family(brief.business_model) == "ecommerce"


def _has_items(brief: Brief) -> bool:
    from engine.render.product_blocks import EVENT_FLOWS
    return brief.flow_type in EVENT_FLOWS


def back_label(brief: Brief) -> str:
    """The button label for a flow whose button returns to the trigger: that cart, or that product."""
    return {"cart_abandon": "Return to my cart", "browse_abandon": "Take another look"}.get(brief.flow_type, "Visit the shop")


def lapse_line(brief: Brief) -> str:
    """Why the re-permission ask is being sent, in the reader's terms."""
    if brief.flow_type == "sunset":
        return "you have not opened or clicked our emails in a while"
    return f"you have not ordered from {P} in a while"


def email_library(brief: Brief) -> Dict[str, List[Dict[str, Any]]]:
    n = brief.churn_threshold_days
    sender = (brief.sender_name or brief.product_name).split(" at ")[0]
    lib: Dict[str, List[Dict[str, Any]]] = {
        "value_reminder": [
            {"subject": f"{n} days since your last order", "preheader": "A plain note, no offer",
             "headline": "Running low?", "paragraphs": [
                 f"Hi {{first_name}}, your last order from {P} was about {n} days ago.",
                 "If you are running low, the button below takes you straight to the shop. If something about that order was not "
                 "right, reply and tell us; a person reads every reply."],
             "cta_label": "Shop again", "framework": "Plain, factual reminder",
             "hypothesis": "A specific, factual gap raises repeat orders within the outcome window more than a benefit line because it "
                           "is concrete; falsified if B's order rate is equal or higher."},
            {"subject": "Order again in a minute", "preheader": f"The quickest way back to {P}",
             "headline": "Pick up where you left off", "paragraphs": [
                 "Hi {first_name}, most people who come back start with what they ordered last time.",
                 "If that is you, the button below takes you straight to the shop. If you want to try something different, reply "
                 "with what you liked and we will suggest one."],
             "cta_label": "Order again", "framework": "BAB: before, after, bridge",
             "hypothesis": "Pointing to the last order lowers the effort of choosing and raises repeat orders; falsified if A converts "
                           "as well or better."},
        ],
        "friction_removal": [
            {"subject": "Was something wrong with your last order?", "preheader": "Tell us and we will make it right",
             "headline": "If something was not right, tell us", "paragraphs": [
                 "Hi {first_name}, when people stop ordering, it is usually one of three things: delivery took too long, something "
                 "arrived not quite right, or it was more than they needed.",
                 "If it was one of those, reply and tell us which; a person will make it right. If not, the shop is one click away."],
             "cta_label": "Visit the shop", "framework": "PAS: problem, agitation kept factual, solution",
             "hypothesis": "Naming the usual reasons and offering a person raises repeat orders among lapsed customers because it "
                           "addresses why they stopped; falsified if B converts as well or better."},
            {"subject": "A smaller order is fine", "preheader": "No need to stock up",
             "headline": "Start small", "paragraphs": [
                 "Hi {first_name}, if your last order was more than you needed, there is no need to stock up this time.",
                 "Order one thing you liked, and come back when you want more."],
             "cta_label": "Order one thing", "framework": "Solution first, problem implied",
             "hypothesis": "Lowering the size of the ask raises repeat orders more than naming the problem; falsified if A converts as "
                           "well or better."},
        ],
        "whats_new": [
            {"subject": f"What is new at {P}", "preheader": "Since your last order",
             "headline": "A quick look at what is new", "paragraphs": [
                 f"Hi {{first_name}}, {P} has kept changing since your last order.",
                 "The quickest way to see what is different is to have a look. It takes a minute."],
             "cta_label": "See what is new", "framework": "AIDA: attention, interest, desire, action",
             "hypothesis": "Inviting a look at what changed raises clicks among lapsed customers because it gives a new reason to come "
                           "back; falsified if B's click rate is equal or higher."},
            {"subject": "A fresh look", "preheader": "If it was not quite right last time",
             "headline": "Worth another look", "paragraphs": [
                 "Hi {first_name}, if your last order was not quite what you wanted, a lot may be different now.",
                 "Have a look, and if it still is not for you, reply and tell us why."],
             "cta_label": "Have a look", "framework": "Reframe: a fresh start, not a reminder",
             "hypothesis": "Framing the return as a fresh look raises clicks more than a what-changed note because it releases a bad "
                           "last experience; falsified if A's click rate is equal or higher."},
        ],
        "human_help": [
            {"subject": "Can we help?", "preheader": f"A note from {sender}",
             "headline": "A quick note", "paragraphs": [
                 f"Hi {{first_name}}, it has been a while since your last order from {P}.",
                 f"If something was not right, or you are not sure what to order next, reply to this email and {sender} will help."],
             "cta_label": "Visit the shop", "framework": "Reciprocity: an offer of help, not a pitch",
             "hypothesis": "A personal offer of help raises replies and repeat orders because it lowers the cost of asking; falsified "
                           "if B gets as many replies."},
            {"subject": f"{sender} from {P}", "preheader": "One question",
             "headline": "One question", "paragraphs": [
                 f"Hi {{first_name}}, what is the one thing that would make {P} worth ordering from again?",
                 "Reply in a line. I read every answer."],
             "cta_label": "Visit the shop", "framework": "Question-led",
             "hypothesis": "Asking one question raises replies more than an offer of help because replying is cheap; falsified if A "
                           "gets as many replies or more."},
        ],
        "use_case": [
            {"subject": "Something you have not tried", "preheader": "Tell us what you liked",
             "headline": "Try something new", "paragraphs": [
                 "Hi {first_name}, if you have been ordering the same thing, there may be something else you would like as much.",
                 "Reply with what you liked last time and we will suggest one."],
             "cta_label": "Visit the shop", "framework": "Open loop closed by a reply",
             "hypothesis": "Inviting a recommendation re-engages customers who only bought one thing; falsified if B converts as well "
                           "or better."},
            {"subject": "One new thing to try", "preheader": "Takes a minute to choose",
             "headline": "Try one new thing", "paragraphs": [
                 f"Hi {{first_name}}, set aside a minute and pick one thing at {P} you have not tried before.",
                 "If it does not suit you, reply and tell us why. We read every answer."],
             "cta_label": "Pick one", "framework": "How-to: one small experiment",
             "hypothesis": "A bounded, one-item ask raises orders more than an open invitation because it removes the decision; "
                           "falsified if A converts as well or better."},
        ],
        "check_in": [
            {"subject": "Quick question", "preheader": "One line is enough",
             "headline": "What would bring you back?", "paragraphs": [
                 f"Hi {{first_name}}, what is one thing {P} would need to do for you to order again?",
                 "Reply with a line. It goes straight to the people who choose what we stock."],
             "cta_label": "Visit the shop", "framework": "Question-led",
             "hypothesis": "A direct question raises replies; falsified if the reply rate is under 1%."},
            {"subject": "Was it something we did?", "preheader": "Honest answers help",
             "headline": "Help us get it right", "paragraphs": [
                 f"Hi {{first_name}}, you have not ordered from {P} for a while, and that usually means we missed something.",
                 "Tell us what it was. A reply is enough."],
             "cta_label": "Visit the shop", "framework": "Question-led, accountability framing",
             "hypothesis": "Taking responsibility raises replies over a neutral question; falsified if A's reply rate is equal or higher."},
        ],
        "repermission": [
            {"subject": "Should we keep sending these?", "preheader": "One click to stay; nothing to do to stop",
             "headline": "Do you still want these emails?", "paragraphs": [
                 f"Hi {{first_name}}, {lapse_line(brief)}, so we want to check before we send more.",
                 "If you want to keep hearing from us, click below. If not, do nothing: in two weeks we will stop these emails. Your "
                 "account and order history stay as they are."],
             "cta_label": "Yes, keep me subscribed", "framework": "Plain re-permission; no guilt, no countdown",
             "hypothesis": "An honest ask keeps the people who still care and lowers complaints from those who do not; expect 1 to 5% "
                           "to click. Falsified if complaints on this send exceed the flow average."},
            {"subject": f"We will pause emails from {P}", "preheader": "Unless you tell us otherwise",
             "headline": "Pausing your emails", "paragraphs": [
                 f"Hi {{first_name}}, {lapse_line(brief)}, so we plan to stop sending you these emails in two weeks.",
                 "Want to keep them? One click does it. Either way, your account and order history stay as they are."],
             "cta_label": "Keep my emails", "framework": "Default-effect framing",
             "hypothesis": "Stating the default (a pause) raises stay clicks over asking a question because it makes the consequence "
                           "concrete; falsified if A's stay rate is equal or higher."},
        ],
        "last_nudge": [
            {"subject": "Whenever you are ready", "preheader": "No rush from us",
             "headline": "Whenever you are ready", "paragraphs": [
                 "Hi {first_name}, you have not placed a first order yet, and that is fine.",
                 "When you are ready, the button below takes you to the shop. If something is in the way, reply and we will help."],
             "cta_label": "Visit the shop", "framework": "Plain reminder",
             "hypothesis": "A low-pressure last reminder converts some undecided subscribers without raising unsubscribes; falsified if "
                           "this send's unsubscribe rate exceeds the flow average."},
            {"subject": "Is now a bad time?", "preheader": "We can wait",
             "headline": "No rush", "paragraphs": [
                 "Hi {first_name}, if now is not the right time for a first order, that is fine.",
                 "When it is, the shop is one click away, and a reply to this email reaches a person."],
             "cta_label": "Visit the shop", "framework": "Permission framing",
             "hypothesis": "Acknowledging timing lowers unsubscribes versus a reminder; falsified if B's unsubscribe rate is equal or higher."},
        ],
    }
    if _has_items(brief):
        cta = back_label(brief)
        lib["social_proof"] = [
            {"subject": "Still deciding on {item_name}?", "preheader": "What other buyers said",
             "headline": "Hear it from other buyers", "paragraphs": [
                 "Hi {first_name}, the people who already bought {item_name} are the best judges of whether it is right for you. "
                 "Tap it below to see what they said.", "{product_block}"],
             "cta_label": cta, "framework": "Social proof: other buyers' verdicts",
             "hypothesis": "Pointing to other buyers at the moment of doubt raises orders within the outcome window; falsified if B "
                           "converts as well or better."},
            {"subject": "What made other people decide", "preheader": "The same question you are weighing",
             "headline": "The same question", "paragraphs": [
                 "Hi {first_name}, most people who stop at this point are weighing one thing: whether it will be worth it.",
                 "The people who went ahead left their verdicts on each product's page. Here is what you were looking at.",
                 "{product_block}"],
             "cta_label": cta, "framework": "Social proof framed as a shared decision",
             "hypothesis": "Naming the reader's hesitation before the proof raises orders more than the proof alone; falsified if A "
                           "converts as well or better."},
        ]
    else:
        lib["social_proof"] = [
            {"subject": "What our regulars order", "preheader": "The picks people come back for",
             "headline": "What people come back for", "paragraphs": [
                 "Hi {first_name}, the products people order again are the best guide to what is worth trying.",
                 "If you want a recommendation, reply and tell us what you like. The button below takes you to the shop."],
             "cta_label": "Visit the shop", "framework": "Social proof through a common pattern",
             "hypothesis": "Pointing to what repeat customers buy raises orders because it lowers the risk of a wrong pick; falsified "
                           "if B converts as well or better."},
            {"subject": "Reviews from people who ordered", "preheader": "The quickest way to choose",
             "headline": "Let other customers help you choose", "paragraphs": [
                 "Hi {first_name}, if you are deciding what to order, the reviews from people who bought each product are the "
                 "quickest way to choose.",
                 "Pick the one with the verdicts that sound most like you."],
             "cta_label": "Read the reviews", "framework": "Social proof through other customers' words",
             "hypothesis": "Sending readers to reviews raises orders more than a curated pick because it lets them judge for "
                           "themselves; falsified if A converts as well or better."},
        ]
    return lib


def short_library(brief: Brief) -> Dict[str, Dict[str, List[Any]]]:
    """SMS, push and in-app versions per role: (A, B). {link} in an SMS is the way back, as in copywriter._short_lib."""
    n = brief.churn_threshold_days
    sender = (brief.sender_name or brief.product_name).split(" at ")[0]
    lib: Dict[str, Dict[str, List[Any]]] = {
        "value_reminder": {
            "sms": [f"{n} days since your last {P} order. Running low? {{link}}", f"Order again from {P} in a minute. {{link}}"],
            "push": [("Running low?", f"Your last order was about {n} days ago."), ("Order again", "Your usual is a minute away.")],
            "in_app": [("Running low?", f"Your last order was about {n} days ago.", "Shop again"),
                       ("Order again", "Start with what you ordered last time.", "Order again")]},
        "friction_removal": {
            "sms": [f"Was something wrong with your last {P} order? Reply and a person will make it right.",
                    f"No need to stock up: order one thing you liked from {P}. {{link}}"],
            "push": [("Something not right?", "Reply to our email and we will make it right."), ("Start small", "Order one thing you liked.")],
            "in_app": [("Something not right?", "Tell us what went wrong and a person will make it right.", "Tell us"),
                       ("Start small", "No need to stock up. Order one thing you liked.", "Shop")]},
        "whats_new": {
            "sms": [f"{P} has changed since your last order. See what is new. {{link}}", f"Worth another look at {P}. {{link}}"],
            "push": [(f"What is new at {P}", "Have a look; it takes a minute."), ("Worth another look", "A lot may be different now.")],
            "in_app": [("What is new", "See what has changed since your last order.", "See what is new"),
                       ("Worth another look", "A lot may be different since your last order.", "Have a look")]},
        "social_proof": {
            "sms": [f"Still deciding? See what other buyers said. {{link}}", f"Other {P} customers left their verdicts. {{link}}"],
            "push": [("Hear it from other buyers", "See what people who bought it said."), ("Still deciding?", "Other buyers left their verdicts.")],
            "in_app": [("Hear it from other buyers", "See what people who bought it said.", "See reviews"),
                       ("Still deciding?", "Other buyers left their verdicts on each product.", "See reviews")]},
        "human_help": {
            "sms": [f"Hi {{first_name}}, it's {sender} at {P}. Anything I can help with? Just reply.",
                    f"{sender} from {P}: what would make us worth ordering from again? Reply in a line."],
            "push": [("Can we help?", f"Reply to {sender}'s email with anything."), (f"One question from {sender}", f"What would bring you back to {P}?")],
            "in_app": [("Can we help?", f"Reply to {sender}'s email and we will help.", "Get help"),
                       ("Tell us what is missing", f"One line on what would bring you back to {P}.", "Answer")]},
        "use_case": {
            "sms": [f"Ordering the same thing from {P}? Reply with what you liked and we will suggest one.",
                    f"One new thing to try at {P}. {{link}}"],
            "push": [("Try something new", "Tell us what you liked and we will suggest one."), ("One new thing", "Pick one you have not tried.")],
            "in_app": [("Try something new", "Tell us what you liked and we will suggest one.", "Tell us"),
                       ("One new thing", "Pick one thing you have not tried before.", "Pick one")]},
        "check_in": {
            "sms": [f"{P}: what would make you order again? Reply with one line.", f"{P} here. Was it something we did? A one-line reply helps."],
            "push": [("Quick question", f"What would bring you back to {P}?"), ("Help us get it right", "Tell us what we missed.")],
            "in_app": [("Quick question", f"What would bring you back to {P}?", "Answer"),
                       ("Help us get it right", "Tell us what we missed. One line is enough.", "Tell us")]},
        "repermission": {
            "sms": [f"{P}: still want these texts? Reply YES to keep them; otherwise we will stop in 2 weeks.",
                    f"{P}: we will pause these texts in 2 weeks unless you reply YES."],
            "push": [("Keep getting updates?", "Tap to keep them; otherwise we will pause them."), ("Pausing updates", "Tap to keep them coming.")],
            "in_app": [("Still want our emails?", "One click keeps them coming. Otherwise we will pause them in two weeks.", "Keep my emails"),
                       ("Pausing your emails", "We plan to stop marketing emails in two weeks. Want them?", "Keep them")]},
        "last_nudge": {
            "sms": [f"Whenever you are ready, {P} is one tap away. {{link}}", "No rush. Questions before a first order? Reply here."],
            "push": [("Whenever you are ready", "Your first order is one tap away."), ("No rush", "Questions? Reply to our email.")],
            "in_app": [("Whenever you are ready", "Your first order is one click away.", "Visit the shop"),
                       ("No rush", "Questions before a first order? Tell us.", "Ask us")]},
    }
    return lib
