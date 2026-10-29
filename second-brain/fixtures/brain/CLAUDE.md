# Hannah's brain

You keep the brain of Hannah Moreau, an independent retail consultant. Her client is a 42-store grocery chain.
Everything she captures lands in `inbox/`: meeting notes, call transcripts and client decks. You file it.

## Filing a file from inbox/

1. Read the whole file. Read a PDF with the Read tool.
2. Write one note per inbox file, at `notes/<the inbox file's name without its extension>.md`.
3. Skip a file that already has its note.
4. Never edit, move or delete anything in `inbox/`.

## The shape of a note

```
---
source: inbox/<the file's full name>
date: <YYYY-MM-DD the meeting or document happened>
people: [<every person the file names>]
---
# <what the file decided or found, in one line>

## Facts

- "<a sentence copied word for word from the file>"
```

- Every fact is a quote copied exactly from the file, in straight double quotes. Never reword a fact,
  never join two sentences, and never write a fact the file does not say.
- Keep every decision, number, date, budget, name and condition. Leave out greetings and small talk.
- Write no other section.

## The index

After filing, `index.md` has one line per note, newest first: `- [<title>](notes/<name>.md) <date>`.
