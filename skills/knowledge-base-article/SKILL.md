---
name: knowledge-base-article
description: Generate well-structured knowledge base articles (KBAs) in markdown. Use when the user asks to create, write, or generate a KBA, knowledge article, knowledge doc, technical article, or learning document on any topic. Triggers on phrases like "generate a kba", "write a knowledge article", "create a kba on", "write me a doc about", or "I want to learn about X - write it up".
---

# Knowledge Base Article Generator

Generate comprehensive, well-written markdown knowledge base articles for personal learning and reference.

## Workflow

1. Determine the topic and classify its type
2. Select the appropriate structure template
3. Research and write the article
4. Save to the `docs/` directory

## Step 1: Classify the Topic

Determine which type best fits the request:

- **How-To / Procedural** — installing, configuring, setting up, deploying
- **Conceptual / Explainer** — understanding architectures, comparing technologies, design patterns
- **Troubleshooting / Diagnostic** — debugging, resolving errors, incident response
- **Reference / Cheat Sheet** — command lists, API summaries, configuration options

Read `references/structure-templates.md` for the detailed template matching the type. Adapt sections freely — not every topic needs every section. Remove sections that don't apply; add sections that do.

## Step 2: Write the Article

Follow these quality standards:

### Title
Use a clear, specific title. Prefer "Installing Kubernetes on Ubuntu 24.04 with kubeadm" over "Kubernetes Installation".

### Table of Contents
Always include a table of contents after the title using markdown links:

```markdown
## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Section Name](#section-name)
```

### Overview
Open with 2-4 sentences: what this article covers, who it's for, and what the reader will know or be able to do afterward.

### Content Guidelines

- **Be specific and concrete.** Use exact commands, real config snippets, and actual version numbers rather than placeholders.
- **Explain the why.** Don't just list steps — explain why each step matters and what it accomplishes.
- **Use code blocks liberally.** Always specify the language for syntax highlighting. Include expected output where helpful.
- **Call out gotchas.** Use blockquotes for warnings and tips:
  ```markdown
  > **Warning:** This will overwrite existing configuration.

  > **Tip:** Run `kubectl get nodes` to verify the cluster is healthy.
  ```
- **Prefer depth over breadth.** A thorough article on one topic is more useful than a shallow one covering many.
- **Include practical examples.** Show real-world usage, not just theory.
- **Link to authoritative sources.** Reference official documentation in a References section at the end.

### Length
Aim for thorough coverage. A good KBA is typically 200-800 lines depending on complexity. Don't pad, but don't cut corners.

## Step 3: Save the Article

Save the article as a markdown file in the `docs/` directory relative to the current working directory. Create the directory if it doesn't exist.

**Filename convention:** lowercase, hyphens for spaces, descriptive.
- `docs/installing-kubernetes-with-kubeadm.md`
- `docs/understanding-dns-resolution.md`
- `docs/troubleshooting-docker-networking.md`

Confirm the file path to the user after saving.
