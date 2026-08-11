---
name: topic-intro
description: >
  Generate a comprehensive introduction markdown document for learning about any
  subject or topic. Use when the user wants to learn about a new topic, asks for
  an introduction to a subject, requests a learning guide, study guide, or primer,
  or says things like "introduce me to X", "I want to learn about X", "create a
  learning doc for X", "topic intro on X", or "help me get started with X".
---

# Topic Introduction Generator

Generate a comprehensive introduction markdown document for a given subject.

## Workflow

1. Ask the user where to save the file if not already specified.
2. Research the topic using web search to ensure accuracy and up-to-date information.
3. Generate the markdown document following the structure below.
4. Write the file and confirm with the user.

## Output Structure

Produce a markdown document with these sections:

```
# Introduction to [Topic]

## Overview
Brief explanation of what the topic is and why it matters (2-3 paragraphs).

## Prerequisites
What the reader should already know or have before diving in.
Use a bulleted list. If none, state "No prerequisites required."

## Key Concepts
Core ideas and terminology. Each concept gets a ### heading with a
1-2 paragraph explanation. Aim for 5-8 key concepts depending on
topic complexity.

## How It Works
Explain the mechanics, architecture, or underlying principles.
Use diagrams (mermaid code blocks) where they aid understanding.

## Getting Started
Practical first steps for hands-on exploration. Include concrete
actions, commands, or exercises where applicable.

## Common Patterns and Best Practices
Typical approaches, idioms, or conventions practitioners follow.

## Common Pitfalls
Mistakes beginners frequently make and how to avoid them.

## Learning Path
Ordered progression from beginner to advanced, with milestones:
1. **Beginner** — foundational topics and resources
2. **Intermediate** — deeper skills and projects
3. **Advanced** — expert-level topics and challenges

## Recommended Resources
Curated list organized by type:
- **Books**
- **Online Courses**
- **Documentation**
- **Communities**
- **Tools**
Include only well-known, reputable resources.

## Glossary
Alphabetical list of key terms with short definitions.
```

## Guidelines

- Use web search to verify facts, current best practices, and resource links.
- Tailor depth to the topic: a narrow technical topic needs more detail per concept; a broad field needs broader coverage.
- Keep language accessible — assume the reader is intelligent but new to the topic.
- Use concrete examples and analogies to explain abstract concepts.
- Pick a sensible filename based on the topic (e.g., `intro-to-rust.md`, `machine-learning-intro.md`, `getting-started-with-kubernetes.md`).
- Do not pad sections. If a section isn't relevant to the topic, omit it.
