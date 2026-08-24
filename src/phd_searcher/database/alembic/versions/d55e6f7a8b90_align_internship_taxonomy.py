"""align existing positions with the complete internship taxonomy

Revision ID: d55e6f7a8b90
Revises: ae91c6b04d72
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d55e6f7a8b90"
down_revision: str | None = "ae91c6b04d72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # La prima migrazione copriva i termini più comuni. Questa usa lo stesso
    # vocabolario del classificatore applicativo, includendo anche stagiaire e
    # research trainee, così PostgreSQL, Qdrant, Review ed export concordano.
    op.execute(
        """
        UPDATE positions
        SET position_type = 'internship',
            screening_status = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'eligible'
                ELSE screening_status
            END,
            screening_reason = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'recognized_type:internship'
                ELSE screening_reason
            END,
            screening_source = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'rules'
                ELSE screening_source
            END,
            screening_confidence = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 1.0
                ELSE screening_confidence
            END,
            screening_version = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN 'taxonomy:internship-v1'
                ELSE screening_version
            END,
            screened_at = CASE
                WHEN screening_manual IS FALSE AND screening_status IN ('pending', 'review')
                    THEN CURRENT_TIMESTAMP
                ELSE screened_at
            END,
            indexed_at = NULL
        WHERE position_type = 'other'
          AND title ~* (
              '(^|[^[:alpha:]])(intern(ship)?|traineeship|research trainee|'
              'tirocin(io|ante)|stagiaire|praktik(um|ant(in)?)|stageplaats|'
              'onderzoeksstage)([^[:alpha:]]|$)|offre de stage|'
              'stage (de|en|di|in) '
              '(recherche|ricerca|research|laboratoire|laboratory)|'
              '(prácticas?|pasantía) de investigación|'
              'estágio de (investigação|pesquisa)'
          )
        """
    )


def downgrade() -> None:
    # Non è possibile distinguere in modo affidabile questi record dalle
    # internship raccolte nativamente dopo l'upgrade: il dato resta valido.
    pass
