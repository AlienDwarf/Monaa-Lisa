from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declarative_base
from pgvector.sqlalchemy import Vector

db_base = declarative_base()

"""
26-July-2025 - Lenio
Abstract: Represents a paper in the database.
Parameters:
- id: Primary key of the paper.
- entry_id: Unique identifier for the paper in arXiv.
- title: Title of the paper.
- authors: Comma-separated string of authors.
- abstract: Abstract of the paper.
- published: Date and time when the paper was published.
- url: URL to the paper's PDF.
- hash: Unique hash of the paper.
- related_papers: JSON field for related papers.
- citations: JSON field for citations of the paper.
- tsne1: First t-SNE coordinate for visualization.
- tsne2: Second t-SNE coordinate for visualization.
- embedding: JSON field for the paper's embedding.
- added: Date and time when the paper was added to the database.
"""
class DBPaper(db_base):
    __tablename__ = "paper"
    id = Column(Integer, primary_key=True, index=True)
    entry_id = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=False)
    authors = Column(String, nullable=True)
    abstract = Column(Text, nullable=True)
    categories = Column(String, nullable=True)
    published = Column(DateTime, nullable=True)
    updated = Column(DateTime, nullable=True)
    doi = Column(String, nullable=True)
    journal_ref = Column(String, nullable=True)
    license = Column(String, nullable=True)
    url = Column(String, nullable=True)
    s2_id = Column(String, nullable=True, unique=True, index=True)
    non_arxiv_citation_count = Column(Integer, nullable=True)
    non_arxiv_reference_count = Column(Integer, nullable=True)
    related_arxiv_ids = Column(JSON, nullable=True)
    # App-only fields (not in dataset, used by ML pipeline)
    tsne = Column(JSON, nullable=True)
    # Abgeleitete Spalten (siehe Prisma-Migration category_list_and_citation_count):
    # Kategorien als Array für exakte Filter, erste Kategorie, Anzahl arXiv-Zitationen
    primary_category = Column(String, nullable=True)
    category_list = Column(ARRAY(String), nullable=True)
    arxiv_citation_count = Column(Integer, nullable=False, default=0)

"""
21-August-2025 - Lenio
Updated 30 09 Nico
Abstract: Represents an embedding in the database.
Parameters:
- content: The content of the embedding, stored as a JSON object.
"""
class DBEmbedding(db_base):
    __tablename__ = "embedding"
    id = Column(Integer, primary_key=True, autoincrement=True)
    belonging_paper_entry_id = Column(String, ForeignKey("paper.entry_id"), index=True, unique=True, nullable=False)
    content = Column(Vector(128), nullable=False)


"""
24-December-2025 - Lenio
Abstract: Represents a citation link between two papers in the database.
Parameters:
- belonging_paper_entry_id: The ID of the paper containing the citation.
- cited_paper_entry_id: The ID of the cited paper.
"""
class DBPaperCitation(db_base):
    __tablename__ = "paper_citation"
    belonging_paper_entry_id = Column(String, ForeignKey("paper.entry_id"), primary_key=True)
    cited_paper_entry_id = Column(String, primary_key=True)  # No FK – cited paper may not exist in DB

"""
24-December-2025 - Lenio
Abstract: Represents a reference link between two papers in the database.
Parameters:
- belonging_paper_entry_id: The ID of the paper containing the reference.
- referenced_paper_entry_id: The ID of the referenced paper.
"""
class DBPaperReference(db_base):
    __tablename__ = "paper_reference"
    belonging_paper_entry_id = Column(String, ForeignKey("paper.entry_id"), primary_key=True)
    referenced_paper_entry_id = Column(String, primary_key=True)  # No FK – referenced paper may not exist in DB


"""
Abstract: Holds papers that are too new for SemanticScholar (no embeddings yet).
    These papers are retried periodically. After max_retries failed attempts
    the entry is dropped.
Parameters:
- entry_id: The arXiv entry_id of the paper.
- title: Title of the paper.
- authors: Comma-separated string of authors.
- abstract: Abstract of the paper.
- categories: arXiv category string.
- published: Publication datetime.
- url: PDF URL.
- retry_count: Number of times we tried SemanticScholar without success.
- max_retries: After this many failures the paper is dropped.
- last_checked: Timestamp of the most recent retry attempt.
- created_at: When the paper was first added to the uncaught table.
"""
class DBUncaughtPaper(db_base):
    __tablename__ = "uncaught_paper"
    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=True)
    authors = Column(String, nullable=True)
    abstract = Column(Text, nullable=True)
    categories = Column(String, nullable=True)
    published = Column(DateTime, nullable=True)
    url = Column(String, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=4, nullable=False)
    last_checked = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
