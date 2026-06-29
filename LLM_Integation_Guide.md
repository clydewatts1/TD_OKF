# How to Use Your OKF Bundle with LLMs

Because your metadata is stored as structured Markdown and YAML, LLMs (like Gemini, ChatGPT, and Claude) can parse it reliably.

Below are three practical ways to use your `.zip` bundle.

## 1. Direct Zip Upload (Easiest)

If your LLM interface supports file uploads (for example, Gemini Advanced or ChatGPT Plus), upload the full `.zip` directly in chat.

### How to do it

1. Drag and drop `okf_bundle.zip` into the chat window.
2. Ask the model to start with the root `index.md`.

### Example prompt

> I have attached a zip file containing the Open Knowledge Format (OKF) metadata for my Teradata data lake. Please start by reading the master `index.md` file to understand the available databases. Once you have read it, tell me which database and table I should use to find daily store inventory stock counts.

## 2. Copy and Paste Method (Quick Queries)

If you do not want to upload the full bundle, copy the relevant Markdown content into the prompt.

### How to do it

1. Open `index.md` or a specific table `.md` file.
2. Copy the file contents.
3. Paste into your prompt.

### Example prompt

> You are an expert Teradata Data Engineer. Below is the metadata for a table called `FND_STK_FCT_04_FCT_STG`. Using this schema, write an optimized Teradata SQL query that aggregates total stock quantity (`STK_QTY`) by location (`LOC_WID`) for the last 7 days. Here is the table metadata: [Paste Markdown Here]

## 3. Enterprise RAG (Retrieval-Augmented Generation)

For internal AI assistants (for example, with Vertex AI or LangChain), this directory layout is well-suited for ingestion.

### How to do it

1. Point your document loader at the `tables/` directory.
2. Chunk the Markdown files.
3. Store embeddings in a vector database.
4. Retrieve relevant files at question time and generate SQL with context.

### Example outcome

When someone asks, "How do I join customer to order?", the system can retrieve the relevant table docs and generate join logic using the documented keys.

## Why Including DDL Is a Superpower

Including Teradata `SHOW TABLE` DDL gives the model physical design context, not just column names.

It can use details like:

- `PARTITION BY` definitions
- `UNIQUE PRIMARY INDEX` definitions

This helps the model generate more efficient SQL (for example, filtering on partition columns like `LST_UPD_DT`) and can reduce unnecessary Teradata compute.