"""Dataset-level revision counters used to invalidate Corpus FS state."""

from api.db.db_models import Knowledgebase


class CorpusRevisionService:
    @staticmethod
    def bump_content(dataset_id: str) -> None:
        Knowledgebase.update(content_revision=Knowledgebase.content_revision + 1).where(Knowledgebase.id == dataset_id).execute()

    @staticmethod
    def bump_acl(dataset_id: str) -> None:
        Knowledgebase.update(acl_revision=Knowledgebase.acl_revision + 1).where(Knowledgebase.id == dataset_id).execute()
