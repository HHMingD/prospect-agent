# The studio's site agent: use case

## What it is

An agent that lives on a marketing studio's website and gets to know each visitor by what they do, without knowing who
they are. It speaks first, only when it has something worth saying, and it decides for itself when to look again and
how much effort a visitor is worth. It is not a live chat and it never asks "how can I help you?". It is honest that it
cannot help properly yet: "I don't know you, so here's something useful about what you're reading." Its aim is to earn
a name and an email, so the studio's CRM can take over with everything the agent has learned.

It is the studio's marketing, shown rather than described: witty, self-aware, willing to critique the studio's own
page, and handing out facts, ideas and fresh things the website doesn't mention.

## What it knows: the payload

Everything arrives anonymously, keyed to a first-party cookie.

Per event:
- visitor id, session id, visit number, time
- page and section, time on the previous page, scroll depth, where they came from (search, LinkedIn, a campaign tag,
  direct), coarse device, country and local time
- what they touched: a case-study filter, the pricing toggle, a video, an image, copied text, a link out
- reactions to the agent: "tell me more", "not for me", dismissed, ignored
- signal elements the page includes on purpose to learn more: "show me work for: DTC / B2B / retail", "what's your
  biggest headache?" chips, a "roast my site" field

Per visitor, kept across visits (its memory):
- the full timeline: every event, every remark the agent made, and what happened next
- what the agent believes about them so far, and how sure it is: "probably DTC, paid social, price-sensitive"
- what it has already said, so it never repeats itself
- what it is preparing for them, and when and why it plans to look again
- once they give a name and email: the enrichment (role, company, industry, size, news) and its research on their
  business

The anonymous history joins the person once they identify themselves. The CRM receives the lot.

## How it decides: its own wakes

1. **A cheap look on every meaningful event** (small model, milliseconds of thought). Do I know enough to say something
   useful? Is this a good moment: they paused, they're going back and forth between pricing and work, they're about to
   leave? Then it does one of three things: stay quiet, say one short thing, or decide this visitor is worth more.
2. **The expensive look, only when the cheap one says it's worth it.** It searches the web for what's new in their
   likely world (a platform change, an industry shift), digs into the studio's archive, and once they've given a
   company, enriches them through Apollo and reads their site. Then it brings something they couldn't get from the
   page.
3. **Its own next wake.** "If they're still here in two minutes, look again." "They left mid-thought; when they're
   back, open with what I was preparing." "They gave their email; send the site teardown tomorrow morning, their time."
4. **Keeping its material fresh.** When its stock of interesting things is stale, it decides to go and learn what's new
   in marketing, so it has things the studio's website doesn't say.

Nothing wakes it on a schedule, and no human supervises it.

## What it sounds like

- On the services page after a while: "Nine services on one page. Honestly, most brands only need two of them. Tell me
  what you sell and I'll tell you which two."
- Going back and forth between pricing and case studies: "Comparing price to proof. Sensible. Here's the one number
  from these case studies that actually predicts whether it'd work for you."
- Lingering on a campaign image: a behind-the-scenes fact about that shoot, from the studio's own notes.
- Second visit: "Back again. Last time you were stuck between retainer and project. Here's what nobody tells you about
  retainers."
- About to leave: "I can't help you properly; I don't know who you are. Leave a name and your brand, and by tomorrow
  I'll send you three things I'd change on your homepage."
- After they identify themselves: a fun fact about their own brand or category, a short, witty critique of their site,
  and something new in their industry they may have missed.

Every fact comes from the studio's material or a source it found. It never makes anything up and never fakes knowing
the visitor.

## How it grows with the visitor

- **First pages:** it only watches.
- **A pattern:** one remark about what they're reading.
- **A reaction:** sharper, more personal remarks.
- **A name and email:** research on their actual business.
- **The CRM:** the studio's people take over, with the whole story.

That gradual build-up, decided by the agent at each step, is the proactivity.

## What this demonstrates, in order

1. **Our agent-building:** a genuinely engaging, intelligent agent, not a chat box.
2. **That we understood the studio's problem:** anonymous visitors leave silently, and this turns them into known,
   understood leads while showing off the studio's thinking.
3. **Minutehand as the debugging tool:** simulated visitors navigating the site over many visits. Did it stay quiet
   when it knew too little? Did it spend on research only when it paid off? Did it get the email? Did it ever make
   something up? (Minutehand can't simulate a person browsing a website yet; that would need adding.)
