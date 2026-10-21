"""Offline starter copy for the flow library's roles beyond win-back and activation.

Two structurally different variants per role, each with a framework and a falsifiable hypothesis, written to be sendable
without fill-ins: what the starter cannot know is carried by tokens the renderer fills from the platform's own data
({first_name}, the brand as {product_name}, the first item as {item_name}, and the item block as a {product_block}
paragraph), never by [brackets]. No starter mentions an offer or a deadline: those depend on the offer policy, and the
guardrails block them where the policy does not allow them. Claude, or a marketer, should still make every line specific;
these are a floor, not a ceiling.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from engine.models import Brief, base_form

P = "{product_name}"


def _h(text: str) -> str:
    return text


def email_library(brief: Brief) -> Dict[str, List[Dict[str, Any]]]:
    core = brief.core_conversion_event
    act = core.label or core.event.replace("_", " ")
    do = base_form(act)
    trial = brief.trial_days or 14
    return {
        "welcome_promise": [
            {"subject": f"Welcome to {P}", "preheader": "What to expect, and the best place to start",
             "headline": "Glad you are here",
             "paragraphs": ["Hi {first_name}, thanks for joining. Here is what we promise: we will only email when there is something "
                            "worth your time, and every email has one clear next step.",
                            f"Most people start in the same place. The button below takes you there."],
             "cta_label": "Start here", "framework": "Promise and proof",
             "hypothesis": "Stating the email promise up front raises first-week clicks because it sets an expectation the reader can trust; "
                           "falsified if B's click rate is equal or higher."},
            {"subject": "{first_name}, here is where most people start", "preheader": "One step, about two minutes",
             "headline": "Start with one thing",
             "paragraphs": [f"Hi {{first_name}}, there is a lot in {P}. You do not need all of it.",
                            "Start with the one thing most new customers choose first. If it is not right for you, reply and tell us."],
             "cta_label": "See where to start", "framework": "Curated first step",
             "hypothesis": "Naming a single first step raises the goal rate within 14 days more than a brand welcome because it removes the "
                           "choice; falsified if A converts as well or better."},
        ],
        "best_sellers": [
            {"subject": "What most people pick first", "preheader": "The safe first choice, and why",
             "headline": "Where most people start",
             "paragraphs": ["Hi {first_name}, if you are not sure where to begin, start with the picks most new customers choose first.",
                            "They are on one page, and each one is a safe first choice. If you want help choosing, reply to this email."],
             "cta_label": "Shop the favorites", "framework": "Curated choice: reduce options to the safe first pick",
             "hypothesis": "Showing the most-chosen products raises first orders because it lowers the risk of a wrong pick; falsified if B "
                           "converts as well or better."},
            {"subject": "Not sure what to choose?", "preheader": "A short guide to the first order",
             "headline": "A short guide to choosing",
             "paragraphs": [f"Hi {{first_name}}, the question we hear most from new customers is where to start with {P}.",
                            "Our answer: start small, with something you will use every day. The usual first choices are one click away."],
             "cta_label": "Help me choose", "framework": "Question-led guidance",
             "hypothesis": "Leading with the reader's own question raises clicks more than a product list because it meets them at the "
                           "decision; falsified if A's click rate is equal or higher."},
        ],
        "how_it_works": [
            {"subject": f"How {P} works, in three steps", "preheader": "What you get, when, and what if it is not right",
             "headline": "Three things to know",
             "paragraphs": ["Hi {first_name}, first: you choose what you want and when. Second: we tell you when it ships and when it "
                            "arrives.",
                            "Third: if something is not right, reply to any email and a person sorts it out."],
             "cta_label": "Get started", "framework": "Risk reversal through clarity",
             "hypothesis": "Answering delivery and returns questions before they are asked raises first orders; falsified if B converts as "
                           "well or better."},
            {"subject": "The questions new customers ask us", "preheader": "Shipping, timing, and what if it is not right",
             "headline": "Your questions, answered",
             "paragraphs": ["Hi {first_name}, before a first order most people want to know three things: how long it takes, what it "
                            "costs to ship, and what happens if it is not right.",
                            "All three answers are one click away, on one page."],
             "cta_label": "Read the answers", "framework": "FAQ as reassurance",
             "hypothesis": "Framing the email as answers raises clicks among undecided subscribers; falsified if A's click rate is equal or higher."},
        ],
        "first_order_nudge": [
            {"subject": "Still deciding?", "preheader": "No rush. Here is the easiest place to start",
             "headline": "Whenever you are ready",
             "paragraphs": ["Hi {first_name}, no pressure from us. When you are ready for your first order, the button below takes you to "
                            "the picks most people start with.",
                            "Questions first? Reply and a person will answer."],
             "cta_label": "Browse the favorites", "framework": "Plain reminder",
             "hypothesis": "A low-pressure reminder converts some undecided subscribers without raising unsubscribes; falsified if this "
                           "send's unsubscribe rate exceeds the flow average."},
            {"subject": "A quick question, {first_name}", "preheader": "What is holding you back?",
             "headline": "What would help?",
             "paragraphs": ["Hi {first_name}, you joined a little while ago and have not ordered yet. That is completely fine.",
                            "If something is holding you back, a price question, a product question, anything, reply with one line and we "
                            "will answer it."],
             "cta_label": "Shop now", "framework": "Question-led objection discovery",
             "hypothesis": "Inviting a reply surfaces objections and converts some readers; falsified if B's order rate is equal to or "
                           "below A's and replies are under 1%."},
        ],
        "cart_reminder": [
            {"subject": "You left something in your cart", "preheader": "It is saved for you",
             "headline": "Your cart is saved",
             "paragraphs": ["Hi {first_name}, you left {item_name} in your cart. We saved it, so you can pick up right where you stopped.",
                            "{product_block}"],
             "cta_label": "Return to checkout", "framework": "Plain reminder: show the cart",
             "hypothesis": "Showing the exact items raises recovered orders within 7 days because it restores intent; falsified if B "
                           "recovers as many or more."},
            {"subject": "Did something go wrong at checkout?", "preheader": "Your cart is still here",
             "headline": "Still thinking it over?",
             "paragraphs": ["Hi {first_name}, it looks like checkout did not finish. If something went wrong, reply and we will sort it out.",
                            "If you were just busy, your cart is right where you left it.", "{product_block}"],
             "cta_label": "Finish checking out", "framework": "Service framing",
             "hypothesis": "Offering help with a failed checkout recovers more orders than a plain reminder because some abandons are "
                           "errors; falsified if A recovers as many or more."},
        ],
        "cart_objection": [
            {"subject": "Questions about your order?", "preheader": "Shipping, delivery and returns, answered",
             "headline": "Before you decide",
             "paragraphs": ["Hi {first_name}, people usually stop at checkout for one of three reasons: the shipping cost, when it will "
                            "arrive, or what happens if it is not right.",
                            "The answers to all three are one click away. Your cart is saved below.", "{product_block}"],
             "cta_label": "See the answers", "framework": "Objection handling: the specific answer, not reassurance",
             "hypothesis": "Answering checkout objections recovers more carts than a second reminder; falsified if B recovers as many or more."},
            {"subject": "Here is what other customers say", "preheader": "About the items in your cart",
             "headline": "Still deciding?",
             "paragraphs": ["Hi {first_name}, if you are weighing whether {item_name} is right for you, the reviews from people who bought "
                            "it are the fastest way to decide.", "{product_block}"],
             "cta_label": "Read the reviews", "framework": "Social proof at the point of doubt",
             "hypothesis": "Pointing to reviews raises recovered orders among hesitant shoppers; falsified if A recovers as many or more."},
        ],
        "cart_last_call": [
            {"subject": "Your cart is still saved", "preheader": "One last reminder from us",
             "headline": "Last reminder",
             "paragraphs": ["Hi {first_name}, this is our last note about your cart. Everything is still saved if you want it.",
                            "{product_block}", "If now is not the time, no problem. We will not email about this cart again."],
             "cta_label": "Complete my order", "framework": "Plain last reminder",
             "hypothesis": "A clear last reminder that promises no more emails recovers orders without raising unsubscribes; falsified if "
                           "this send's unsubscribe rate exceeds the flow average."},
            {"subject": "Should we keep your cart?", "preheader": "Just checking",
             "headline": "Keep it or let it go?",
             "paragraphs": ["Hi {first_name}, your cart is still here, with {item_name} in it.",
                            "If you still want it, the button below takes you straight to checkout. If not, simply ignore this email."],
             "cta_label": "Keep my cart", "framework": "Permission framing",
             "hypothesis": "Asking whether to keep the cart lowers unsubscribes versus a reminder at equal recovery; falsified if B's "
                           "unsubscribe rate is equal or higher."},
        ],
        "browse_reminder": [
            {"subject": "Still thinking about {item_name}?", "preheader": "Here it is again",
             "headline": "Take another look",
             "paragraphs": ["Hi {first_name}, you were looking at this recently. Here it is again, in case you want a second look.",
                            "{product_block}"],
             "cta_label": "Take another look", "framework": "Relevance: their own choice, shown back",
             "hypothesis": "Showing the viewed product raises orders within 7 days; falsified if B converts as well or better."},
            {"subject": "Questions about what you were viewing?", "preheader": "We can help you decide",
             "headline": "Need help deciding?",
             "paragraphs": ["Hi {first_name}, if you were comparing options, reply and tell us what matters to you. A person will point you "
                            "to the right one.", "{product_block}"],
             "cta_label": "See it again", "framework": "Service framing",
             "hypothesis": "Offering help raises clicks among comparison shoppers more than a plain reminder; falsified if A's click rate is "
                           "equal or higher."},
        ],
        "browse_alternatives": [
            {"subject": "A few options to compare", "preheader": "Side by side",
             "headline": "Compare before you choose",
             "paragraphs": ["Hi {first_name}, people who look at {item_name} often compare it with a couple of others.",
                            "We put them side by side on one page, so you can choose in a minute."],
             "cta_label": "Compare them", "framework": "Comparison that ends in a recommendation",
             "hypothesis": "A side-by-side comparison raises orders among undecided viewers; falsified if B converts as well or better."},
            {"subject": "Most people choose this one", "preheader": "And here is why",
             "headline": "The usual choice",
             "paragraphs": ["Hi {first_name}, if you are deciding between options, most customers who looked at {item_name} chose it in "
                            "the end. Here it is again.", "{product_block}"],
             "cta_label": "See why", "framework": "Default choice",
             "hypothesis": "Naming the common choice raises orders more than a comparison because it removes the decision; falsified if A "
                           "converts as well or better."},
        ],
        "order_thanks": [
            {"subject": "Thank you for your first order", "preheader": "What happens next",
             "headline": "Thank you",
             "paragraphs": ["Hi {first_name}, thank you for your first order with us. We will email you when it ships, and again when it "
                            "is on its way to you.",
                            "If anything is not right when it arrives, reply to this email and a person will fix it."],
             "cta_label": "See your order", "framework": "Reassurance and expectation setting",
             "hypothesis": "Setting delivery expectations lowers support contacts and raises second orders; falsified if B's second-order "
                           "rate is equal or higher."},
            {"subject": "Getting the most from your order", "preheader": "One tip for day one",
             "headline": "Welcome aboard",
             "paragraphs": ["Hi {first_name}, thanks for ordering. One tip before it arrives: the first use sets the tone, so we wrote a "
                            "short guide to getting it right.",
                            "It takes two minutes to read."],
             "cta_label": "Read the guide", "framework": "Education from day one",
             "hypothesis": "A first-use guide raises second orders more than a thank-you note; falsified if A converts as well or better."},
        ],
        "how_to_use": [
            {"subject": "One tip for {item_name}", "preheader": "Small change, better results",
             "headline": "A small tip",
             "paragraphs": ["Hi {first_name}, now that your order has arrived, here is the one tip customers tell us made the biggest "
                            "difference.", "It is on one short page, with pictures."],
             "cta_label": "See the tip", "framework": "Education that increases value delivered",
             "hypothesis": "A usage tip raises second orders within 30 days because value drives repurchase; falsified if B converts as "
                           "well or better."},
            {"subject": "How is it going so far?", "preheader": "Tell us in one line",
             "headline": "How is it going?",
             "paragraphs": ["Hi {first_name}, you have had your order for about a week. How is it going?",
                            "Reply with one line. If something is not right, we will fix it; if it is, we would love to know."],
             "cta_label": "Tell us", "framework": "Check-in",
             "hypothesis": "A check-in surfaces problems early and lowers refunds; falsified if B's refund rate is equal or higher than A's."},
        ],
        "review_request": [
            {"subject": "How did we do?", "preheader": "One click to tell us",
             "headline": "Would you share a review?",
             "paragraphs": ["Hi {first_name}, an honest review helps the next person decide, and helps us get better.",
                            "It takes about a minute, and you can say whatever you like."],
             "cta_label": "Write a review", "framework": "Reciprocity, honestly asked",
             "hypothesis": "A plain, honest ask yields more reviews than a product-focused one; falsified if B's review rate is equal or higher."},
            {"subject": "What did you think of {item_name}?", "preheader": "Your review helps other customers",
             "headline": "Your opinion counts",
             "paragraphs": ["Hi {first_name}, you have had {item_name} for a couple of weeks now. What did you think?",
                            "A short review helps other people choose well."],
             "cta_label": "Leave a review", "framework": "Specific ask about the product",
             "hypothesis": "Naming the product raises the review rate because it makes the ask concrete; falsified if A's review rate is "
                           "equal or higher."},
        ],
        "cross_sell": [
            {"subject": "Goes well with your last order", "preheader": "A natural next pick",
             "headline": "A good pairing",
             "paragraphs": ["Hi {first_name}, customers who bought what you bought often add one more thing next.",
                            "We put the most common pairings on one page."],
             "cta_label": "See the pairing", "framework": "Complement, not upsell",
             "hypothesis": "Showing complements raises second orders within 30 days; falsified if B converts as well or better."},
            {"subject": "Ready for the next one?", "preheader": "What people usually order second",
             "headline": "What comes next",
             "paragraphs": ["Hi {first_name}, the second order is usually where people find their favorite.",
                            "The most common second picks are one click away."],
             "cta_label": "Find my next favorite", "framework": "Journey framing",
             "hypothesis": "Framing the second order as a next step raises it more than a pairing; falsified if A converts as well or better."},
        ],
        "replenish_reminder": [
            {"subject": "Running low on {item_name}?", "preheader": "Reorder in one click",
             "headline": "Time for more?",
             "paragraphs": ["Hi {first_name}, based on your last order, you are probably getting close to the end of it.",
                            "{product_block}", "Reorder in one click, and it ships the same way as last time."],
             "cta_label": "Reorder now", "framework": "Timely reminder anchored on their own usage",
             "hypothesis": "A reminder timed to the customer's supply raises reorders within 14 days of the due date; falsified if B "
                           "converts as well or better."},
            {"subject": "Before you run out", "preheader": "Your usual, ready to reorder",
             "headline": "Your usual is ready",
             "paragraphs": ["Hi {first_name}, we set up your usual order so it is one click away.", "{product_block}"],
             "cta_label": "Order my usual", "framework": "Convenience framing",
             "hypothesis": "Framing the reorder as already prepared raises reorders more than a reminder; falsified if A converts as well "
                           "or better."},
        ],
        "replenish_nudge": [
            {"subject": "Still need more {item_name}?", "preheader": "A quick reminder",
             "headline": "A quick reminder",
             "paragraphs": ["Hi {first_name}, just a reminder in case the last email got buried. Your usual is one click away.",
                            "{product_block}"],
             "cta_label": "Reorder", "framework": "Plain reminder",
             "hypothesis": "A second reminder catches people who meant to reorder and forgot; falsified if this touch's orders are under 10% "
                           "of the first touch's."},
            {"subject": "Never run out again", "preheader": "Set it and forget it",
             "headline": "Make it automatic",
             "paragraphs": ["Hi {first_name}, if you order the same thing regularly, you can set it to arrive on a schedule and change or "
                            "pause it anytime.", "{product_block}"],
             "cta_label": "See the options", "framework": "Convenience framing",
             "hypothesis": "Suggesting a schedule raises repeat orders over 60 days more than a reminder; falsified if A's 60-day repeat "
                           "rate is equal or higher."},
        ],
        "replenish_last": [
            {"subject": "Last reminder about your reorder", "preheader": "Then we will stop asking",
             "headline": "One last reminder",
             "paragraphs": ["Hi {first_name}, this is our last reminder about reordering. If you have moved on, no problem.",
                            "{product_block}"],
             "cta_label": "Reorder", "framework": "Plain last reminder",
             "hypothesis": "A last reminder that promises no more emails converts some lapsed buyers without raising unsubscribes; "
                           "falsified if this send's unsubscribe rate exceeds the flow average."},
            {"subject": "Was it not quite right?", "preheader": "Tell us in one line",
             "headline": "Was something off?",
             "paragraphs": ["Hi {first_name}, you have not reordered, which is fine. If it was not quite right, reply and tell us why.",
                            "We read every reply."],
             "cta_label": "Shop again", "framework": "Question-led",
             "hypothesis": "Asking why surfaces product problems and converts some customers; falsified if replies are under 1% and B's "
                           "orders are not above A's."},
        ],
        "payment_failed": [
            {"subject": "Your payment did not go through", "preheader": "Update your payment details in a minute",
             "headline": "Your payment did not go through",
             "paragraphs": ["Hi {first_name}, we tried to take your latest payment for " + P + " and it did not go through. This usually "
                            "means a card expired or was replaced.",
                            "Nothing is lost. Update your payment details and you are set."],
             "cta_label": "Update payment details", "framework": "Plain notice: what, why, what to do",
             "hypothesis": "Naming the likely cause (an expired card) raises same-day updates; falsified if B's update rate within 24 hours "
                           "is equal or higher."},
            {"subject": "Action needed: update your payment details", "preheader": "Your account is still active",
             "headline": "One quick fix",
             "paragraphs": ["Hi {first_name}, your latest payment for " + P + " was declined. Your account is still active.",
                            "It takes about a minute to update your card."],
             "cta_label": "Update my card", "framework": "Plain notice, reassurance first",
             "hypothesis": "Reassuring that the account is active lowers cancellations during dunning; falsified if B's cancellation rate is "
                           "equal or higher than A's."},
        ],
        "payment_retry": [
            {"subject": "Reminder: your payment details need updating", "preheader": "We will try again soon",
             "headline": "A quick reminder",
             "paragraphs": ["Hi {first_name}, your payment details for " + P + " still need updating. We will try the payment again soon.",
                            "Updating now means nothing changes for you."],
             "cta_label": "Update payment details", "framework": "Plain notice with the next step",
             "hypothesis": "A reminder before the retry raises recovery; falsified if this touch adds under 5% of recoveries."},
            {"subject": "Still need to update your card", "preheader": "It takes about a minute",
             "headline": "Still pending",
             "paragraphs": ["Hi {first_name}, we have not been able to take your payment for " + P + " yet.",
                            "If you have a question about the charge, reply and a person will help."],
             "cta_label": "Update my card", "framework": "Service framing",
             "hypothesis": "Inviting questions raises recovery among confused customers; falsified if A recovers as many or more."},
        ],
        "payment_final": [
            {"subject": "Final notice: your payment is still due", "preheader": "Update before access pauses",
             "headline": "Final notice",
             "paragraphs": ["Hi {first_name}, we still have not been able to take your payment for " + P + ". If it is not updated, access "
                            "will pause on the date shown in your account.",
                            "Your data is kept either way."],
             "cta_label": "Keep my account active", "framework": "Plain final notice with the billing system's date",
             "hypothesis": "A clear final notice recovers the remaining willing payers; falsified if this touch adds under 5% of recoveries."},
            {"subject": "Before your access pauses", "preheader": "One step keeps everything as it is",
             "headline": "Keep everything as it is",
             "paragraphs": ["Hi {first_name}, one step keeps your " + P + " account exactly as it is: update your payment details.",
                            "If you meant to cancel, you do not need to do anything."],
             "cta_label": "Update payment details", "framework": "Permission framing",
             "hypothesis": "Acknowledging that some people mean to cancel lowers complaints at equal recovery; falsified if B's complaint "
                           "rate is equal or higher."},
        ],
        "access_paused": [
            {"subject": "Your access is paused", "preheader": "Restore it in one step",
             "headline": "Your access is paused",
             "paragraphs": ["Hi {first_name}, we could not take your payment, so access to " + P + " is paused. Your data is kept.",
                            "Update your payment details and everything comes back as it was."],
             "cta_label": "Restore access", "framework": "Plain notice with a clear way back",
             "hypothesis": "A clear way back recovers paused customers within 7 days; falsified if B recovers as many or more."},
            {"subject": "Everything is saved for you", "preheader": "Come back anytime",
             "headline": "We kept everything",
             "paragraphs": ["Hi {first_name}, your " + P + " account is paused, not deleted. Everything you set up is saved.",
                            "Whenever you are ready, one step restores it."],
             "cta_label": "Restore my account", "framework": "Loss framing kept honest",
             "hypothesis": "Stressing that nothing is lost raises restores more than a plain notice; falsified if A recovers as many or more."},
        ],
        "trial_welcome": [
            {"subject": f"Your {trial}-day trial has started", "preheader": f"The fastest way to see what {P} does",
             "headline": "Welcome to your trial",
             "paragraphs": [f"Hi {{first_name}}, your trial runs for {trial} days. The fastest way to see what " + P + f" can do is to {do}.",
                            "It takes a few minutes, and everything you set up stays if you choose a plan."],
             "cta_label": "Take the first step", "framework": "BAB: before, after, bridge",
             "hypothesis": "One first step in the welcome raises the goal rate within the trial; falsified if B converts as well or better."},
            {"subject": "Start here, {first_name}", "preheader": "One step that shows the value",
             "headline": "Start with one thing",
             "paragraphs": ["Hi {first_name}, most people who end up loving " + P + f" start by doing one thing: they {do}.",
                            "The button below takes you straight there."],
             "cta_label": "Start now", "framework": "Social proof through common behavior",
             "hypothesis": "Framing the first step as what successful users do raises it more than a trial welcome; falsified if A "
                           "converts as well or better."},
        ],
        "trial_value": [
            {"subject": "Halfway through your trial", "preheader": "What you have done, and what is next",
             "headline": "Halfway there",
             "paragraphs": ["Hi {first_name}, you are halfway through your trial. The one thing most people who choose a plan have done by "
                            f"now is {act}.", "If you have not yet, now is a good time."],
             "cta_label": "Do it now", "framework": "Progress plus next best action",
             "hypothesis": "A midpoint nudge toward the key action raises paid conversion; falsified if B converts as well or better."},
            {"subject": "Is " + P + " working for you?", "preheader": "Tell us in one line",
             "headline": "How is the trial going?",
             "paragraphs": ["Hi {first_name}, you are about halfway through your trial. Is it doing what you hoped?",
                            "Reply with one line. If something is missing, we will tell you honestly whether " + P + " can do it."],
             "cta_label": "Open " + P, "framework": "Check-in",
             "hypothesis": "An honest check-in raises paid conversion among engaged users; falsified if A converts as well or better."},
        ],
        "trial_ending": [
            {"subject": "Your trial ends in 3 days", "preheader": "What happens next, and your options",
             "headline": "Three days left",
             "paragraphs": ["Hi {first_name}, your trial ends in three days, on the date shown in your account. After that, your account "
                            "pauses until you choose a plan.",
                            "Everything you set up is kept. The button below shows the plans."],
             "cta_label": "See the plans", "framework": "Plain notice with a real date",
             "hypothesis": "A plain, dated notice converts decided users without raising complaints; falsified if B's paid conversion is "
                           "equal or higher at an equal complaint rate."},
            {"subject": "Keep everything you set up", "preheader": "Your trial ends in 3 days",
             "headline": "Keep your work",
             "paragraphs": ["Hi {first_name}, in three days your trial ends. Choosing a plan keeps everything exactly as it is.",
                            "Not sure which plan fits? Reply and we will recommend one based on how you use " + P + "."],
             "cta_label": "Choose a plan", "framework": "Loss framing kept honest",
             "hypothesis": "Framing the choice as keeping their work raises paid conversion more than a notice; falsified if A converts as "
                           "well or better."},
        ],
        "trial_last_day": [
            {"subject": "Your trial ends tomorrow", "preheader": "Choose a plan to keep going",
             "headline": "Last day tomorrow",
             "paragraphs": ["Hi {first_name}, your trial ends tomorrow. Choose a plan today to keep going without a break.",
                            "Everything you set up stays."],
             "cta_label": "Choose a plan", "framework": "Plain reminder with a real date",
             "hypothesis": "A last-day reminder converts undecided users; falsified if this touch adds under 10% of conversions."},
            {"subject": "One day left, {first_name}", "preheader": "Questions before you decide?",
             "headline": "Any questions?",
             "paragraphs": ["Hi {first_name}, your trial ends tomorrow. If a question is holding you back, reply and a person will "
                            "answer today."],
             "cta_label": "See the plans", "framework": "Service framing at the decision",
             "hypothesis": "Offering a same-day answer raises last-day conversion; falsified if A converts as well or better."},
        ],
        "trial_ended": [
            {"subject": "Your trial has ended", "preheader": "Your work is saved",
             "headline": "Your trial has ended",
             "paragraphs": ["Hi {first_name}, your trial ended, and your account is paused. Everything you set up is saved.",
                            "Choose a plan whenever you are ready and you will pick up exactly where you left off."],
             "cta_label": "Choose a plan", "framework": "Plain notice",
             "hypothesis": "A plain notice after the trial converts late deciders; falsified if this touch adds under 5% of conversions."},
            {"subject": "Was something missing?", "preheader": "Tell us in one line",
             "headline": "What would have made it work?",
             "paragraphs": ["Hi {first_name}, your trial ended without a plan. That is useful to us too: what was missing?",
                            "Reply with one line. We read every answer."],
             "cta_label": "Choose a plan", "framework": "Question-led",
             "hypothesis": "Asking what was missing yields product research and some conversions; falsified if replies are under 1% and B "
                           "does not convert more than A."},
        ],
        "repermission_reminder": [
            {"subject": "Do you still want our emails?", "preheader": "One click to stay",
             "headline": "Still interested?",
             "paragraphs": ["Hi {first_name}, a week ago we asked whether you still want to hear from " + P + ". We have not heard back.",
                            "Click below to stay. If we do not hear from you, we will stop emailing you in a week. You can always "
                            "sign up again."],
             "cta_label": "Keep me subscribed", "framework": "Plain reminder with a stated consequence",
             "hypothesis": "A reminder with a stated stop date raises re-permission clicks; falsified if this touch adds under 10% of the "
                           "first touch's clicks."},
            {"subject": "We will stop emailing you soon", "preheader": "Unless you want us to keep going",
             "headline": "Should we stop?",
             "paragraphs": ["Hi {first_name}, you have not opened a link from us in a long while, so we plan to stop emailing you next week.",
                            "If you want to keep hearing from us, one click is all it takes."],
             "cta_label": "Keep my emails", "framework": "Plain re-permission; no guilt, no countdown",
             "hypothesis": "Stating the plan to stop raises clicks among people who still want the emails; falsified if A's click rate is "
                           "equal or higher."},
        ],
    }


def short_library(brief: Brief) -> Dict[str, Dict[str, List[Any]]]:
    """SMS, push and in-app versions per role: (A, B). SMS strings get the brand prefix and STOP language added for them."""
    core = brief.core_conversion_event
    do = base_form(core.label or core.event.replace("_", " "))
    Do = do[:1].upper() + do[1:]
    generic = lambda title, body, cta: {  # noqa: E731
        "sms": [f"{body} {{product_name}}.", f"{title}: {body}"],
        "push": [(title, body), (title, body)],
        "in_app": [(title, body, cta), (title, body, cta)]}
    return {
        "welcome_promise": generic("Welcome", "Thanks for joining. Here is the best place to start", "Start here"),
        "best_sellers": generic("Where most people start", "The picks new customers choose first", "See them"),
        "how_it_works": generic("How it works", "What you get, when, and what if it is not right", "Learn more"),
        "first_order_nudge": generic("Whenever you are ready", "The easiest place to start your first order", "Shop now"),
        "cart_reminder": {"sms": ["You left something in your cart. It is saved for you.", "Your cart is saved. Pick up where you stopped."],
                          "push": [("Your cart is saved", "Pick up right where you stopped."), ("Still thinking it over?", "Your cart is waiting.")],
                          "in_app": [("Your cart is saved", "Pick up right where you stopped.", "View cart"),
                                     ("Still deciding?", "Your cart is waiting for you.", "View cart")]},
        "cart_objection": {"sms": ["Questions about shipping or returns? Reply here and we will answer.",
                                   "Your cart is saved. Shipping and returns answers are on our site."],
                           "push": [("Questions about your order?", "Shipping, delivery and returns, answered."),
                                    ("Before you decide", "Read what other customers say.")],
                           "in_app": [("Questions about your order?", "Shipping, delivery and returns, answered.", "See answers"),
                                      ("Before you decide", "Read what other customers say.", "Read reviews")]},
        "cart_last_call": generic("Your cart is still saved", "Last reminder about your cart", "Complete order"),
        "browse_reminder": generic("Take another look", "Here is what you were looking at", "See it"),
        "browse_alternatives": generic("Compare your options", "A few picks people compare side by side", "Compare"),
        "order_thanks": generic("Thank you", "Thanks for your first order. We will tell you when it ships", "View order"),
        "how_to_use": generic("One tip for your order", "The tip customers say made the biggest difference", "See the tip"),
        "review_request": generic("How did we do?", "A short review helps the next customer decide", "Review"),
        "cross_sell": generic("Goes well with your order", "What customers usually add next", "See it"),
        "replenish_reminder": {"sms": ["Running low? Reorder your usual in one click.", "Your usual is ready to reorder whenever you are."],
                               "push": [("Time for more?", "Reorder your usual in one click."), ("Before you run out", "Your usual is one click away.")],
                               "in_app": [("Time for more?", "Reorder your usual in one click.", "Reorder"),
                                          ("Before you run out", "Your usual is one click away.", "Reorder")]},
        "replenish_nudge": {"sms": ["A quick reminder: your usual is one click away.", "Want it on a schedule? You can pause anytime."],
                            "push": [("A quick reminder", "Your usual is one click away."), ("Make it automatic", "Set it to arrive on a schedule.")],
                            "in_app": [("A quick reminder", "Your usual is one click away.", "Reorder"),
                                       ("Make it automatic", "Set it to arrive on a schedule.", "See options")]},
        "replenish_last": generic("One last reminder", "Our last reminder about reordering", "Reorder"),
        "payment_failed": generic("Payment did not go through", "Update your payment details to keep your account active", "Update"),
        "payment_retry": {"sms": ["Your payment details still need updating. It takes a minute.",
                                  "We could not take your payment yet. Update your card to keep things running."],
                          "push": [("Payment still pending", "Update your payment details in a minute."),
                                   ("Action needed", "Your card needs updating.")],
                          "in_app": [("Payment still pending", "Update your payment details in a minute.", "Update"),
                                     ("Action needed", "Your card needs updating.", "Update card")]},
        "payment_final": generic("Final notice", "Update your payment details before access pauses", "Update"),
        "access_paused": generic("Access paused", "Restore your account in one step; your data is kept", "Restore"),
        "trial_welcome": generic("Your trial has started", f"{Do} first; it shows the value fastest", "Start"),
        "trial_value": {"sms": [f"Halfway through your trial. Most people who stay have done this: {do}.", "How is your trial going? Reply with one line."],
                        "push": [("Halfway through your trial", f"{Do} if you have not yet."), ("How is it going?", "Tell us in one line.")],
                        "in_app": [("Halfway through your trial", f"{Do} if you have not yet.", Do[:24]),
                                   ("How is it going?", "Tell us what is missing.", "Answer")]},
        "trial_ending": generic("Trial ends in 3 days", "Choose a plan to keep everything you set up", "See plans"),
        "trial_last_day": {"sms": ["Your trial ends tomorrow. Choose a plan to keep going.", "One day left in your trial. Questions? Reply here."],
                           "push": [("Trial ends tomorrow", "Choose a plan to keep going."), ("One day left", "Questions before you decide?")],
                           "in_app": [("Trial ends tomorrow", "Choose a plan to keep going.", "Choose a plan"),
                                      ("One day left", "Questions before you decide?", "Ask us")]},
        "trial_ended": generic("Your trial has ended", "Your work is saved; choose a plan anytime", "Choose a plan"),
        "repermission_reminder": generic("Still want our emails?", "One click keeps them coming", "Keep them"),
    }
