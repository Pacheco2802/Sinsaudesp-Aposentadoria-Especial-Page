import uuid as _uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Cadastro(Base):
    __tablename__ = "cadastros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome_completo: Mapped[str] = mapped_column(String(255), nullable=False)
    cpf: Mapped[str] = mapped_column(String(14), nullable=False, unique=True, index=True)
    telefone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    hospital: Mapped[str] = mapped_column(String(255), nullable=False)
    cargo: Mapped[str] = mapped_column(String(255), nullable=False)
    tempo_servico: Mapped[str] = mapped_column(String(100), nullable=False)
    filiado: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Dados para a procuração / kit (INSS)
    rg: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    data_nascimento: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    estado_civil: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    nacionalidade: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # True = recebe aposentadoria/pensão de outro regime de previdência
    recebe_outro_beneficio: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Endereço completo
    cep: Mapped[Optional[str]] = mapped_column(String(9), nullable=True)
    logradouro: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    numero: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    complemento: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    bairro: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    cidade: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    uf: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)

    # Atendimento
    analise_estabilidade: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    modalidade_atendimento: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # online | presencial
    agendamento: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Etapa 2 (documentação completa + procuração, liberada após o atendimento)
    etapa2_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    etapa2_liberada_em: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    etapa2_concluida_em: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(50), nullable=False, default="novo", server_default="novo")
    nota_interna: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    zapsign_doc_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_cadastro: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now(), onupdate=func.now())

    atendente_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admin_usuarios.id", ondelete="SET NULL"), nullable=True, index=True
    )
    atendente: Mapped[Optional["AdminUsuario"]] = relationship(
        "AdminUsuario", foreign_keys="[Cadastro.atendente_id]", lazy="selectin"
    )

    documentos: Mapped[list["Documento"]] = relationship(
        "Documento",
        back_populates="cadastro",
        cascade="all, delete-orphan",
    )


class Documento(Base):
    __tablename__ = "documentos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cadastro_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cadastros.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipo: Mapped[str] = mapped_column(String(50), nullable=False)
    nome_arquivo: Mapped[str] = mapped_column(String(255), nullable=False)
    caminho_arquivo: Mapped[str] = mapped_column(String(500), nullable=False)
    tamanho_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    cadastro: Mapped["Cadastro"] = relationship("Cadastro", back_populates="documentos")


class AdminUsuario(Base):
    __tablename__ = "admin_usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    senha_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # "admin" = acesso total (gerencia usuários) | "juridico" = gerencia cadastros
    papel: Mapped[str] = mapped_column(String(20), nullable=False, server_default="juridico")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    cadastros_atribuidos: Mapped[list["Cadastro"]] = relationship(
        "Cadastro", back_populates="atendente", foreign_keys="[Cadastro.atendente_id]"
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    id_publico: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, index=True,
        default=lambda: str(_uuid.uuid4()),
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    consentimento_termos: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consentimento_marketing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cadastro_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("cadastros.id", ondelete="SET NULL"), nullable=True, index=True
    )
    convertido_em: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    lembrete_1_enviado_em: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    lembrete_2_enviado_em: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    descadastrado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class EventoSessao(Base):
    __tablename__ = "eventos_sessao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class ConfigAgenda(Base):
    """Configuração global de horários — sempre 1 linha (id=1)."""
    __tablename__ = "config_agenda"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    hora_inicio: Mapped[str] = mapped_column(String(5), nullable=False, server_default="09:00")
    hora_fim: Mapped[str] = mapped_column(String(5), nullable=False, server_default="16:00")
    intervalo_minutos: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    atualizado_em: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    atualizado_por: Mapped[str] = mapped_column(String(255), nullable=False, server_default="sistema")


class BloqueioAgenda(Base):
    """Datas específicas sem atendimento."""
    __tablename__ = "bloqueios_agenda"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    motivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    criado_por: Mapped[str] = mapped_column(String(255), nullable=False, server_default="sistema")


class AgendaSlotOverride(Base):
    """Ajustes finos de horário por dia, sobrepostos à grade padrão.

    tipo="extra"    → adiciona um horário que não existe na grade (encaixe).
    tipo="removido" → desabilita um horário da grade naquele dia específico.
    """
    __tablename__ = "agenda_slot_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    hora: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"
    tipo: Mapped[str] = mapped_column(String(10), nullable=False)  # "extra" | "removido"
    criado_em: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    criado_por: Mapped[str] = mapped_column(String(255), nullable=False, server_default="sistema")

    __table_args__ = (
        UniqueConstraint("data", "hora", name="uq_slot_override_data_hora"),
    )


class NotaCadastro(Base):
    """Notas internas em formato de feed (várias por cadastro, com autor e data)."""
    __tablename__ = "notas_cadastro"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cadastro_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cadastros.id", ondelete="CASCADE"), nullable=False, index=True
    )
    autor_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("admin_usuarios.id", ondelete="SET NULL"), nullable=True
    )
    autor_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    autor_nome: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    criado_em: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class HistoricoCadastro(Base):
    """Auditoria de ações feitas sobre um cadastro (status, atendente, etapa 2, edições)."""
    __tablename__ = "historico_cadastro"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cadastro_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cadastros.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # status_alterado | atendente_atribuido | atendente_removido | etapa2_liberada
    # | etapa2_concluida | dados_editados | cadastro_criado
    tipo: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ator_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ator_nome: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    descricao: Mapped[str] = mapped_column(Text, nullable=False)
    valor_anterior: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    valor_novo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
