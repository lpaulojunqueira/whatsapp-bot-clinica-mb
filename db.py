"""
Módulo de banco de dados.
Centraliza todas as operações no PostgreSQL.
Estrutura pensada pra multi-tenant desde o início.
"""

import os
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL")


# ============================================================
# CONEXÃO
# ============================================================
def _conectar():
    """Abre uma nova conexão com o banco."""
    return psycopg.connect(DATABASE_URL)


# ============================================================
# CRIAÇÃO DAS TABELAS + SEED
# ============================================================
def inicializar_banco():
    """
    Cria as tabelas se ainda não existem.
    Roda toda vez que o servidor sobe — é seguro (não duplica nada).
    Também cadastra a clínica MB se for a primeira execução.
    """
    conn = _conectar()
    cur = conn.cursor()

    # Tabela de clínicas — cada cliente do produto é uma linha aqui.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clinicas (
            id SERIAL PRIMARY KEY,
            nome TEXT NOT NULL,
            phone_number_id TEXT UNIQUE NOT NULL,
            system_prompt TEXT NOT NULL,
            telefone_humano TEXT,
            whatsapp_token TEXT,
            criada_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # Garante a coluna do token mesmo em bancos que já existiam antes.
    cur.execute("""
        ALTER TABLE clinicas
        ADD COLUMN IF NOT EXISTS whatsapp_token TEXT;
    """)

    # Rastreamento de conversões da Meta (Conversions API) por tenant.
    # Vazio / capi_ativo=false = rastreamento desligado, sem erro.
    cur.execute("""
        ALTER TABLE clinicas
        ADD COLUMN IF NOT EXISTS meta_dataset_id TEXT;
    """)
    cur.execute("""
        ALTER TABLE clinicas
        ADD COLUMN IF NOT EXISTS meta_capi_token TEXT;
    """)
    cur.execute("""
        ALTER TABLE clinicas
        ADD COLUMN IF NOT EXISTS capi_ativo BOOLEAN DEFAULT FALSE;
    """)
    cur.execute("""
        ALTER TABLE clinicas
        ADD COLUMN IF NOT EXISTS meta_test_event_code TEXT;
    """)
    # ID da Página do Facebook associada ao dataset/anúncios do tenant.
    # A Meta EXIGE esse campo nos eventos de mensagem (erro 2804116 sem ele).
    cur.execute("""
        ALTER TABLE clinicas
        ADD COLUMN IF NOT EXISTS meta_page_id TEXT;
    """)

    # Follow-up proativo (lembrete pré-consulta + reativação de lead frio).
    # Cada tipo só dispara se tiver nome de template preenchido (e followup_ativo).
    for coluna, tipo in [
        ("followup_ativo", "BOOLEAN DEFAULT FALSE"),
        ("followup_lembrete_hora", "TIME DEFAULT '08:00'"),
        ("followup_template_lembrete", "TEXT"),
        ("followup_template_frio", "TEXT"),
        # Números extras reconhecidos como DONO (além do telefone_humano principal).
        # Um por linha/vírgula. Só afeta a detecção de modo dono, nada mais.
        ("telefones_humanos_extras", "TEXT"),
        # Profissionais dividem a mesma sala: um agendamento ocupa a sala pra
        # todos (bloqueios continuam pessoais). Default FALSE = agendas independentes.
        ("sala_compartilhada", "BOOLEAN DEFAULT FALSE"),
        # ID da conta WhatsApp Business (WABA) — necessário pra criar templates de
        # campanha via API de gerenciamento da Meta. Diferente do phone_number_id.
        ("waba_id", "TEXT"),
    ]:
        cur.execute(f"ALTER TABLE clinicas ADD COLUMN IF NOT EXISTS {coluna} {tipo};")

    # Tabela de conversas — uma por (clínica, lead).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversas (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            numero_lead TEXT NOT NULL,
            criada_em TIMESTAMPTZ DEFAULT NOW(),
            atualizada_em TIMESTAMPTZ DEFAULT NOW(),
            pausada BOOLEAN DEFAULT FALSE,
            UNIQUE(clinica_id, numero_lead)
        );
    """)

    # Caso a tabela já exista de antes, garante a coluna nova.
    cur.execute("""
        ALTER TABLE conversas
        ADD COLUMN IF NOT EXISTS pausada BOOLEAN DEFAULT FALSE;
    """)

    # Atribuição de anúncio click-to-WhatsApp (referral). Capturado SEMPRE,
    # mesmo com CAPI desligado — o dado de origem é valioso por si.
    for coluna, tipo in [
        ("ctwa_clid", "TEXT"),
        ("referral_source_id", "TEXT"),
        ("referral_source_type", "TEXT"),
        ("referral_source_url", "TEXT"),
        ("referral_headline", "TEXT"),
        ("referral_body", "TEXT"),
        ("referral_json", "JSONB"),
        ("referral_captado_em", "TIMESTAMPTZ"),
    ]:
        cur.execute(
            f"ALTER TABLE conversas ADD COLUMN IF NOT EXISTS {coluna} {tipo};"
        )

    # Lead pediu pra não receber mais follow-up (opt-out).
    cur.execute("""
        ALTER TABLE conversas
        ADD COLUMN IF NOT EXISTS followup_optout BOOLEAN DEFAULT FALSE;
    """)

    # Tabela de mensagens — histórico completo.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mensagens (
            id SERIAL PRIMARY KEY,
            conversa_id INT NOT NULL REFERENCES conversas(id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            conteudo TEXT NOT NULL,
            criada_em TIMESTAMPTZ DEFAULT NOW(),
            message_id_whatsapp TEXT UNIQUE
        );
    """)

    # Tabela de usuários do painel (login).
    # clinica_id NULL = admin (vê todas as clínicas).
    # clinica_id preenchido = usuário daquela clínica (vê só dela).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            nome TEXT,
            clinica_id INT REFERENCES clinicas(id),
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # =====================================================
    # SISTEMA DE AGENDA
    # =====================================================
    # Configuração de horários por clínica.
    # 1 linha por clínica. Se não existir, é criada com padrões ao consultar.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config_horarios (
            clinica_id INT PRIMARY KEY REFERENCES clinicas(id),
            duracao_minutos INT NOT NULL DEFAULT 60,
            antecedencia_minima_minutos INT NOT NULL DEFAULT 180,
            dias_semana TEXT NOT NULL DEFAULT '1,2,3,4,5',
            hora_inicio TIME NOT NULL DEFAULT '09:00',
            hora_fim TIME NOT NULL DEFAULT '18:00',
            almoco_inicio TIME,
            almoco_fim TIME,
            atualizada_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # Agendamentos confirmados (ou cancelados / realizados / no_show).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agendamentos (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            conversa_id INT REFERENCES conversas(id),
            numero_lead TEXT NOT NULL,
            nome_lead TEXT,
            data_hora TIMESTAMPTZ NOT NULL,
            duracao_minutos INT NOT NULL DEFAULT 60,
            status TEXT NOT NULL DEFAULT 'confirmado',
            origem TEXT NOT NULL DEFAULT 'manual',
            observacao TEXT,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            cancelado_em TIMESTAMPTZ,
            confirmacao_24h_enviada BOOLEAN DEFAULT FALSE
        );
    """)
    # Índice pra busca rápida por data (a função "horários livres" usa muito).
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_agendamentos_clinica_data
        ON agendamentos(clinica_id, data_hora)
        WHERE status = 'confirmado';
    """)

    # Bloqueios manuais (feriado, ausência, almoço extra, consulta presencial fora).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bloqueios (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            inicio TIMESTAMPTZ NOT NULL,
            fim TIMESTAMPTZ NOT NULL,
            motivo TEXT,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_bloqueios_clinica_data
        ON bloqueios(clinica_id, inicio, fim);
    """)

    # =====================================================
    # MULTI-PROFISSIONAL
    # =====================================================
    # Profissionais de uma clínica (ex: Dr. Matheus, Dra. Maryah).
    # Clínica SEM profissionais cadastrados = comportamento single antigo
    # (agenda única). O modo multi só liga quando há profissionais ativos.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS profissionais (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            nome TEXT NOT NULL,
            ativo BOOLEAN DEFAULT TRUE,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_profissionais_clinica
        ON profissionais(clinica_id) WHERE ativo = TRUE;
    """)

    # Override de horários por profissional. Se um profissional não tiver linha
    # aqui, herda a config_horarios da clínica (default). Assim clínicas single
    # ficam 100% intactas — nada aqui as afeta.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config_horarios_prof (
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            profissional_id INT NOT NULL REFERENCES profissionais(id),
            duracao_minutos INT NOT NULL DEFAULT 60,
            antecedencia_minima_minutos INT NOT NULL DEFAULT 180,
            dias_semana TEXT NOT NULL DEFAULT '1,2,3,4,5',
            hora_inicio TIME NOT NULL DEFAULT '09:00',
            hora_fim TIME NOT NULL DEFAULT '18:00',
            almoco_inicio TIME,
            almoco_fim TIME,
            atualizada_em TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (clinica_id, profissional_id)
        );
    """)

    # A qual profissional pertence cada agendamento/bloqueio.
    # NULL = clínica single (legado) ou bloqueio que vale pra clínica toda.
    cur.execute("""
        ALTER TABLE agendamentos
        ADD COLUMN IF NOT EXISTS profissional_id INT REFERENCES profissionais(id);
    """)
    cur.execute("""
        ALTER TABLE bloqueios
        ADD COLUMN IF NOT EXISTS profissional_id INT REFERENCES profissionais(id);
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_agendamentos_profissional
        ON agendamentos(clinica_id, profissional_id, data_hora)
        WHERE status = 'confirmado';
    """)

    # Horários próprios pra sábado e domingo (nullable). NULL = usa o horário dos
    # dias úteis (hora_inicio/hora_fim), então clínicas antigas ficam idênticas.
    for _tab in ("config_horarios", "config_horarios_prof"):
        for _col in ("hora_inicio_sabado", "hora_fim_sabado",
                     "hora_inicio_domingo", "hora_fim_domingo"):
            cur.execute(
                f"ALTER TABLE {_tab} ADD COLUMN IF NOT EXISTS {_col} TIME;"
            )

    # Índice pra busca rápida de duplicatas.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_mensagens_msg_id
        ON mensagens(message_id_whatsapp);
    """)

    # Log de eventos enviados pra Conversions API da Meta.
    # event_id UNIQUE garante deduplicação (não reenvia o mesmo evento).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS capi_eventos (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            conversa_id INT REFERENCES conversas(id),
            event_name TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            status TEXT,
            resposta TEXT,
            criado_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # Follow-ups enviados. O UNIQUE serve de trava: garante 1 envio por
    # (tipo, referência, tentativa), mesmo com vários workers rodando o agendador.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS followups (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            tipo TEXT NOT NULL,
            ref_tipo TEXT NOT NULL,
            ref_id INT NOT NULL,
            tentativa INT NOT NULL DEFAULT 1,
            status TEXT,
            resposta TEXT,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (tipo, ref_tipo, ref_id, tentativa)
        );
    """)

    # Vendas registradas manualmente (fecho de contrato/compra). Dispara Purchase.
    # conversa_id é opcional: venda avulsa (cliente que veio por fora, sem conversa
    # no sistema) também conta no faturamento, só não tem atribuição de anúncio.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vendas (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            conversa_id INT REFERENCES conversas(id),
            valor NUMERIC(12,2),
            moeda TEXT DEFAULT 'BRL',
            descricao TEXT,
            registrada_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    # Bancos que já criaram a tabela com NOT NULL: libera a venda avulsa.
    cur.execute("ALTER TABLE vendas ALTER COLUMN conversa_id DROP NOT NULL;")

    # Campanhas de disparo em massa (template WhatsApp). template_status:
    # rascunho | em_aprovacao | aprovado | reprovado | enviando | concluida | erro.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS campanhas (
            id SERIAL PRIMARY KEY,
            clinica_id INT NOT NULL REFERENCES clinicas(id),
            etapa TEXT NOT NULL,
            dias INT NOT NULL DEFAULT 90,
            mensagem TEXT NOT NULL,
            template_nome TEXT,
            template_status TEXT NOT NULL DEFAULT 'rascunho',
            template_motivo TEXT,
            orcamento NUMERIC(12,2),
            alcance_alvo INT,
            criado_em TIMESTAMPTZ DEFAULT NOW(),
            atualizada_em TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    conn.commit()
    cur.close()
    conn.close()

    # Depois das tabelas prontas, popula a clínica MB se necessário.
    _seed_clinica_mb()
    # E o usuário admin (Luiz) se necessário.
    _seed_usuario_admin()


def _seed_clinica_mb():
    """
    Cadastra a clínica MB no banco se ainda não estiver lá.
    Usa o PHONE_NUMBER_ID da variável de ambiente.
    """
    phone_id_mb = os.getenv("PHONE_NUMBER_ID")
    if not phone_id_mb:
        print("⚠️  PHONE_NUMBER_ID não definido — pulando seed da MB.")
        return

    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM clinicas WHERE phone_number_id = %s",
        (phone_id_mb,)
    )
    if cur.fetchone() is None:
        cur.execute(
            """
            INSERT INTO clinicas (nome, phone_number_id, system_prompt, telefone_humano)
            VALUES (%s, %s, %s, %s)
            """,
            ("MB Odontologia", phone_id_mb, PROMPT_MB, "19 99343-6676")
        )
        conn.commit()
        print("✅ Clínica MB cadastrada no banco.")
    cur.close()
    conn.close()


def _seed_usuario_admin():
    """
    Cria o usuário admin se ainda não existe.
    Usa ADMIN_EMAIL e ADMIN_PASSWORD das variáveis de ambiente.
    Admin = clinica_id NULL = vê todas as clínicas.
    """
    from werkzeug.security import generate_password_hash
    email = os.getenv("ADMIN_EMAIL")
    senha = os.getenv("ADMIN_PASSWORD")
    if not email or not senha:
        print("⚠️  ADMIN_EMAIL/ADMIN_PASSWORD não definidos — pulando seed do admin.")
        return

    conn = _conectar()
    cur = conn.cursor()
    cur.execute("SELECT id FROM usuarios WHERE email = %s", (email.lower(),))
    if cur.fetchone() is None:
        cur.execute(
            """
            INSERT INTO usuarios (email, senha_hash, nome, clinica_id)
            VALUES (%s, %s, %s, NULL)
            """,
            (email.lower(), generate_password_hash(senha), "Admin")
        )
        conn.commit()
        print(f"✅ Usuário admin '{email}' cadastrado.")
    cur.close()
    conn.close()


# ============================================================
# OPERAÇÕES DE LEITURA E ESCRITA
# ============================================================
def buscar_clinica_por_phone_id(phone_number_id):
    """Identifica QUAL clínica recebeu a mensagem (multi-tenant)."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT * FROM clinicas WHERE phone_number_id = %s",
        (phone_number_id,)
    )
    clinica = cur.fetchone()
    cur.close()
    conn.close()
    return clinica


def obter_ou_criar_conversa(clinica_id, numero_lead):
    """Pega o ID da conversa (clínica + lead) ou cria uma nova."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM conversas WHERE clinica_id = %s AND numero_lead = %s",
        (clinica_id, numero_lead)
    )
    row = cur.fetchone()
    if row:
        conversa_id = row[0]
        cur.execute(
            "UPDATE conversas SET atualizada_em = NOW() WHERE id = %s",
            (conversa_id,)
        )
    else:
        cur.execute(
            """
            INSERT INTO conversas (clinica_id, numero_lead)
            VALUES (%s, %s) RETURNING id
            """,
            (clinica_id, numero_lead)
        )
        conversa_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return conversa_id


def obter_conversa(conversa_id):
    """Retorna a linha completa da conversa (inclui campos de referral) ou None."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM conversas WHERE id = %s", (conversa_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def buscar_conversa_por_numero(clinica_id, numero):
    """
    Acha a conversa mais recente de um lead pelo número, comparando os últimos
    10 dígitos (ignora DDI e formatação). Retorna a linha ou None.
    """
    import re as _re
    digitos = _re.sub(r"\D", "", numero or "")
    if len(digitos) < 8:
        return None
    ultimos = digitos[-10:] if len(digitos) >= 10 else digitos
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT * FROM conversas
        WHERE clinica_id = %s
          AND RIGHT(regexp_replace(numero_lead, '\\D', '', 'g'), %s) = %s
        ORDER BY atualizada_em DESC
        LIMIT 1
        """,
        (clinica_id, len(ultimos), ultimos)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def salvar_referral_conversa(conversa_id, referral):
    """
    Salva os dados de atribuição do anúncio (referral do click-to-WhatsApp) na
    conversa. First-touch: só grava se a conversa ainda não tiver referral, pra
    preservar a origem original. Idempotente e seguro chamar em toda mensagem.
    """
    if not referral:
        return
    from psycopg.types.json import Json
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE conversas SET
            ctwa_clid = %s,
            referral_source_id = %s,
            referral_source_type = %s,
            referral_source_url = %s,
            referral_headline = %s,
            referral_body = %s,
            referral_json = %s,
            referral_captado_em = NOW()
        WHERE id = %s AND referral_captado_em IS NULL
        """,
        (
            referral.get("ctwa_clid"),
            referral.get("source_id"),
            referral.get("source_type"),
            referral.get("source_url"),
            referral.get("headline"),
            referral.get("body"),
            Json(referral),
            conversa_id,
        )
    )
    conn.commit()
    cur.close()
    conn.close()


# ---------- CONVERSIONS API (log + deduplicação) ----------
def capi_evento_ja_enviado(event_id):
    """True se esse event_id já foi enviado com sucesso (evita reenvio)."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM capi_eventos WHERE event_id = %s AND status = 'enviado' LIMIT 1",
        (event_id,)
    )
    existe = cur.fetchone() is not None
    cur.close()
    conn.close()
    return existe


def registrar_evento_capi(clinica_id, conversa_id, event_name, event_id,
                          status, resposta=None):
    """
    Registra (ou atualiza) o resultado de um evento CAPI. Usa event_id como chave:
    uma tentativa que falhou pode ser reenviada e o registro é atualizado.
    """
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO capi_eventos
            (clinica_id, conversa_id, event_name, event_id, status, resposta)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO UPDATE
          SET status = EXCLUDED.status,
              resposta = EXCLUDED.resposta,
              criado_em = NOW()
        """,
        (clinica_id, conversa_id, event_name, event_id, status,
         (resposta or "")[:2000])
    )
    conn.commit()
    cur.close()
    conn.close()


def listar_agendamentos_para_reenvio_capi(dias=7):
    """
    Agendamentos confirmados criados nos últimos N dias — candidatos a reenvio
    de evento CAPI (a janela de atribuição da Meta é de 7 dias).
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT a.id, a.clinica_id, a.conversa_id, a.numero_lead,
               a.nome_lead, a.criado_em, a.data_hora,
               cl.nome AS clinica_nome
        FROM agendamentos a
        JOIN clinicas cl ON cl.id = a.clinica_id
        WHERE a.status = 'confirmado'
          AND a.criado_em >= NOW() - (%s || ' days')::interval
        ORDER BY a.criado_em ASC
        """,
        (str(int(dias)),)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ============================================================
# FOLLOW-UP (lembrete pré-consulta + reativação de lead frio)
# ============================================================
def followup_claim(clinica_id, tipo, ref_tipo, ref_id, tentativa=1):
    """
    Tenta 'reservar' o envio de um follow-up. Retorna True se reservou (você deve
    enviar), False se outro worker/ciclo já pegou. A restrição UNIQUE da tabela é
    a trava — garante 1 envio mesmo com vários workers rodando o agendador.
    """
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO followups (clinica_id, tipo, ref_tipo, ref_id, tentativa, status)
        VALUES (%s, %s, %s, %s, %s, 'processando')
        ON CONFLICT (tipo, ref_tipo, ref_id, tentativa) DO NOTHING
        RETURNING id
        """,
        (clinica_id, tipo, ref_tipo, ref_id, tentativa)
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row is not None


def followup_resultado(tipo, ref_tipo, ref_id, tentativa, status, resposta=None):
    """Atualiza o status de um follow-up já reservado (enviado / erro)."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE followups SET status = %s, resposta = %s
        WHERE tipo = %s AND ref_tipo = %s AND ref_id = %s AND tentativa = %s
        """,
        (status, (resposta or "")[:1000], tipo, ref_tipo, ref_id, tentativa)
    )
    conn.commit()
    cur.close()
    conn.close()


def marcar_optout_conversa(conversa_id):
    """Marca a conversa como opt-out de follow-up (lead pediu pra parar)."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "UPDATE conversas SET followup_optout = TRUE WHERE id = %s",
        (conversa_id,)
    )
    conn.commit()
    cur.close()
    conn.close()


def listar_agendamentos_lembrete(agora):
    """
    Agendamentos que devem receber lembrete AGORA: consulta hoje (fuso Brasília),
    ainda por vir, já passou a hora configurada do lembrete, cliente com follow-up
    ativo + template de lembrete, e lembrete ainda não enviado.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT a.id, a.clinica_id, a.numero_lead, a.nome_lead, a.data_hora,
               cl.nome AS clinica_nome, cl.phone_number_id, cl.whatsapp_token,
               cl.followup_template_lembrete
        FROM agendamentos a
        JOIN clinicas cl ON cl.id = a.clinica_id
        WHERE cl.followup_ativo = TRUE
          AND cl.followup_template_lembrete IS NOT NULL
          AND cl.followup_template_lembrete <> ''
          AND a.status = 'confirmado'
          AND a.data_hora > %s
          AND (a.data_hora AT TIME ZONE 'America/Sao_Paulo')::date
              = (%s AT TIME ZONE 'America/Sao_Paulo')::date
          AND (%s AT TIME ZONE 'America/Sao_Paulo')::time >= cl.followup_lembrete_hora
          AND NOT EXISTS (
              SELECT 1 FROM followups f
              WHERE f.tipo = 'lembrete' AND f.ref_tipo = 'agendamento'
                AND f.ref_id = a.id
          )
        ORDER BY a.data_hora ASC
        """,
        (agora, agora, agora)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def listar_conversas_frio_t1():
    """
    Conversas de lead frio pra 1ª tentativa: engajou (>=2 msgs), sem agendamento
    futuro, não pausada, não opt-out, em silêncio entre 24h e 7 dias, cliente com
    follow-up ativo + template de frio, e t1 ainda não enviada.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT c.id, c.numero_lead, c.clinica_id, cl.nome AS clinica_nome,
               cl.phone_number_id, cl.whatsapp_token, cl.followup_template_frio
        FROM conversas c
        JOIN clinicas cl ON cl.id = c.clinica_id
        JOIN mensagens m ON m.conversa_id = c.id
        WHERE cl.followup_ativo = TRUE
          AND cl.followup_template_frio IS NOT NULL AND cl.followup_template_frio <> ''
          AND COALESCE(c.pausada, FALSE) = FALSE
          AND COALESCE(c.followup_optout, FALSE) = FALSE
          AND NOT EXISTS (SELECT 1 FROM agendamentos a
                          WHERE a.conversa_id = c.id AND a.status = 'confirmado'
                            AND a.data_hora > NOW())
          AND NOT EXISTS (SELECT 1 FROM followups f
                          WHERE f.tipo = 'frio' AND f.ref_tipo = 'conversa'
                            AND f.ref_id = c.id AND f.tentativa = 1)
        GROUP BY c.id, c.numero_lead, c.clinica_id, cl.nome, cl.phone_number_id,
                 cl.whatsapp_token, cl.followup_template_frio
        HAVING COUNT(m.id) >= 2
           AND MAX(m.criada_em) < NOW() - INTERVAL '24 hours'
           AND MAX(m.criada_em) > NOW() - INTERVAL '7 days'
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def listar_conversas_frio_t2():
    """
    Conversas pra 2ª tentativa de lead frio: t1 enviada há >= 48h, lead NÃO
    respondeu depois do t1, sem agendamento futuro, não pausada/opt-out, e t2
    ainda não enviada.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT c.id, c.numero_lead, c.clinica_id, cl.nome AS clinica_nome,
               cl.phone_number_id, cl.whatsapp_token, cl.followup_template_frio
        FROM conversas c
        JOIN clinicas cl ON cl.id = c.clinica_id
        JOIN mensagens m ON m.conversa_id = c.id
        JOIN followups f1 ON f1.ref_tipo = 'conversa' AND f1.ref_id = c.id
                         AND f1.tipo = 'frio' AND f1.tentativa = 1
                         AND f1.status = 'enviado'
        WHERE cl.followup_ativo = TRUE
          AND cl.followup_template_frio IS NOT NULL AND cl.followup_template_frio <> ''
          AND COALESCE(c.pausada, FALSE) = FALSE
          AND COALESCE(c.followup_optout, FALSE) = FALSE
          AND f1.criado_em < NOW() - INTERVAL '48 hours'
          AND NOT EXISTS (SELECT 1 FROM agendamentos a
                          WHERE a.conversa_id = c.id AND a.status = 'confirmado'
                            AND a.data_hora > NOW())
          AND NOT EXISTS (SELECT 1 FROM followups f
                          WHERE f.tipo = 'frio' AND f.ref_tipo = 'conversa'
                            AND f.ref_id = c.id AND f.tentativa = 2)
        GROUP BY c.id, c.numero_lead, c.clinica_id, cl.nome, cl.phone_number_id,
                 cl.whatsapp_token, cl.followup_template_frio, f1.criado_em
        HAVING MAX(m.criada_em) < f1.criado_em
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def dashboard_resultados(clinica_id, data_inicio, data_fim):
    """
    KPIs do funil de uma clínica no período (dados de primeira mão do próprio
    sistema): conversas, quantas vieram de anúncio, agendamentos, vendas,
    faturamento e a conversão lead→venda. Cada evento é contado pelo seu próprio
    timestamp (conversa=criada_em, agendamento=criado_em, venda=registrada_em).
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)

    cur.execute(
        """
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE ctwa_clid IS NOT NULL) AS de_anuncio,
          COUNT(*) FILTER (WHERE ctwa_clid IS NULL) AS diretas
        FROM conversas
        WHERE clinica_id = %s AND criada_em >= %s AND criada_em < %s
        """,
        (clinica_id, data_inicio, data_fim)
    )
    conv = cur.fetchone()

    cur.execute(
        """
        SELECT COUNT(*) AS n FROM agendamentos
        WHERE clinica_id = %s AND status = 'confirmado'
          AND criado_em >= %s AND criado_em < %s
        """,
        (clinica_id, data_inicio, data_fim)
    )
    agendamentos = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(valor), 0) AS total
        FROM vendas
        WHERE clinica_id = %s AND registrada_em >= %s AND registrada_em < %s
        """,
        (clinica_id, data_inicio, data_fim)
    )
    v = cur.fetchone()

    # Etapa do lead (mesma lógica do Kanban): "novo" = entrou mas não engajou
    # (poucas mensagens, sem agendamento nem venda). Todo o resto = qualificado
    # (engajou de verdade, agendou ou comprou). É complementar: novos + qualif = total.
    cur.execute(
        """
        WITH base AS (
          SELECT c.id,
            (SELECT COUNT(*) FROM mensagens m WHERE m.conversa_id = c.id) AS n_msgs,
            EXISTS (SELECT 1 FROM agendamentos a
                    WHERE a.conversa_id = c.id AND a.status = 'confirmado') AS tem_agend,
            EXISTS (SELECT 1 FROM vendas ve WHERE ve.conversa_id = c.id) AS tem_venda
          FROM conversas c
          WHERE c.clinica_id = %s AND c.criada_em >= %s AND c.criada_em < %s
        )
        SELECT COUNT(*) FILTER (
                 WHERE n_msgs < 4 AND NOT tem_agend AND NOT tem_venda
               ) AS novos
        FROM base
        """,
        (clinica_id, data_inicio, data_fim)
    )
    novos = cur.fetchone()["novos"] or 0

    cur.close()
    conn.close()

    total = conv["total"] or 0
    vendas_n = v["n"] or 0
    qualificados = max(total - novos, 0)
    return {
        "conversas": total,
        "conversas_anuncio": conv["de_anuncio"] or 0,
        "conversas_direto": conv["diretas"] or 0,
        "novos": novos,
        "qualificados": qualificados,
        "agendamentos": agendamentos or 0,
        "vendas": vendas_n,
        "faturamento": float(v["total"] or 0),
        "qualif_pct": round(100.0 * qualificados / total, 1) if total else 0.0,
        "lead_venda_pct": round(100.0 * vendas_n / total, 1) if total else 0.0,
        "agend_conversa_pct": round(100.0 * (agendamentos or 0) / total, 1) if total else 0.0,
    }


def kanban_leads(clinica_id, dias=30, limite=300):
    """
    Leads do período pro Kanban, com a etapa DERIVADA automaticamente do que a
    Ana já fez (dados de primeira mão): comprou > agendou > em atendimento > novo.
    Cada lead traz nome (do agendamento, se houver), número, origem (anúncio/direto),
    data e valor da venda. É o diferencial: pipeline sem ninguém arrastar card.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT c.id AS conversa_id, c.numero_lead, c.criada_em, c.ctwa_clid,
               COALESCE(c.followup_optout, FALSE) AS optout,
               COUNT(m.id) AS n_msgs,
               MAX(m.criada_em) AS ultima_msg,
               ag.nome_lead, ag.data_hora AS agend_data,
               ve.valor AS venda_valor
        FROM conversas c
        LEFT JOIN mensagens m ON m.conversa_id = c.id
        LEFT JOIN LATERAL (
            SELECT nome_lead, data_hora FROM agendamentos
            WHERE conversa_id = c.id AND status = 'confirmado'
            ORDER BY data_hora DESC LIMIT 1
        ) ag ON TRUE
        LEFT JOIN LATERAL (
            SELECT SUM(valor) AS valor FROM vendas WHERE conversa_id = c.id
        ) ve ON TRUE
        WHERE c.clinica_id = %s
          AND c.criada_em >= NOW() - (%s || ' days')::interval
        GROUP BY c.id, c.numero_lead, c.criada_em, c.ctwa_clid,
                 ag.nome_lead, ag.data_hora, ve.valor
        ORDER BY COALESCE(MAX(m.criada_em), c.criada_em) DESC
        LIMIT %s
        """,
        (clinica_id, str(int(dias)), limite)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    leads = []
    for r in rows:
        if r["venda_valor"] is not None:
            etapa = "comprou"
        elif r["agend_data"] is not None:
            etapa = "agendado"
        elif (r["n_msgs"] or 0) >= 4:
            etapa = "atendimento"
        else:
            etapa = "novo"
        leads.append({
            "conversa_id": r["conversa_id"],
            "numero": r["numero_lead"],
            "nome": r["nome_lead"],
            "origem": "anúncio" if r["ctwa_clid"] else "direto",
            "etapa": etapa,
            "optout": bool(r["optout"]),
            "n_msgs": r["n_msgs"] or 0,
            "ultima_msg": r["ultima_msg"].isoformat() if r["ultima_msg"] else None,
            "agend_data": r["agend_data"].isoformat() if r["agend_data"] else None,
            "venda_valor": float(r["venda_valor"]) if r["venda_valor"] is not None else None,
        })
    return leads


# Custo médio por mensagem de MARKETING no Brasil (R$). AJUSTÁVEL — a Meta mudou
# pra cobrança por mensagem em 2025; conferir o valor real no Billing Hub da conta.
CUSTO_MSG_MARKETING_BRL = 0.35


def publico_campanha(clinica_id, etapa, dias=90):
    """
    Público de uma campanha: contatos numa etapa do Kanban (novo/atendimento/
    agendado/comprou), EXCLUINDO quem deu opt-out e quem não tem número. Reusa a
    derivação de etapa do Kanban. Retorna lista de {conversa_id, numero, nome}.
    """
    leads = kanban_leads(clinica_id, dias, limite=100000)
    return [
        {"conversa_id": l["conversa_id"], "numero": l["numero"], "nome": l["nome"]}
        for l in leads
        if l["etapa"] == etapa and not l["optout"] and l["numero"]
    ]


def estimar_campanha(clinica_id, etapa, dias=90, orcamento=None,
                     custo_msg=CUSTO_MSG_MARKETING_BRL):
    """
    Estima o alcance de uma campanha. Se `orcamento` (R$) for passado, o alcance é
    limitado a orcamento/custo_msg. Retorna dict com o tamanho do segmento, custo
    por msg, alcance possível e custo total desse alcance.
    """
    total = len(publico_campanha(clinica_id, etapa, dias))
    if orcamento and orcamento > 0 and custo_msg > 0:
        cabe = int(orcamento // custo_msg)
        alcance = min(total, cabe)
    else:
        alcance = total
    return {
        "segmento": total,
        "custo_msg": round(custo_msg, 2),
        "alcance": alcance,
        "custo_total": round(alcance * custo_msg, 2),
        "orcamento": orcamento,
    }


def criar_campanha(clinica_id, etapa, dias, mensagem, orcamento, alcance_alvo,
                   template_nome, template_status="em_aprovacao"):
    """Salva uma campanha. Retorna o id."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO campanhas
             (clinica_id, etapa, dias, mensagem, template_nome, template_status,
              orcamento, alcance_alvo)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (clinica_id, etapa, int(dias), mensagem, template_nome, template_status,
         orcamento, alcance_alvo)
    )
    cid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return cid


def atualizar_status_campanha(campanha_id, status, motivo=None):
    """Atualiza o status do template/campanha (e o motivo, se reprovado)."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """UPDATE campanhas SET template_status = %s, template_motivo = %s,
               atualizada_em = NOW() WHERE id = %s""",
        (status, motivo, campanha_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def obter_campanha(campanha_id):
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM campanhas WHERE id = %s", (campanha_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def listar_campanhas(clinica_id):
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT * FROM campanhas WHERE clinica_id = %s ORDER BY criado_em DESC LIMIT 50",
        (clinica_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def dashboard_conversas_por_dia(clinica_id, data_inicio, data_fim):
    """Conversas iniciadas por dia (fuso Brasília) no período — pro gráfico."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT (criada_em AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
               COUNT(*) AS n
        FROM conversas
        WHERE clinica_id = %s AND criada_em >= %s AND criada_em < %s
        GROUP BY 1 ORDER BY 1
        """,
        (clinica_id, data_inicio, data_fim)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"dia": r["dia"].isoformat(), "n": r["n"]} for r in rows]


def dashboard_capi_resumo(clinica_id, data_inicio, data_fim):
    """
    Quantos eventos CAPI foram ENVIADOS com sucesso (Meta respondeu 200) vs erro,
    por tipo, no período. É a régua pra comparar 'nosso número' com o que de fato
    saiu pra Meta e diagnosticar o gap de contabilização.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT event_name,
               COUNT(*) FILTER (WHERE status = 'enviado') AS enviados,
               COUNT(*) FILTER (WHERE status <> 'enviado') AS erros
        FROM capi_eventos
        WHERE clinica_id = %s AND criado_em >= %s AND criado_em < %s
        GROUP BY event_name
        """,
        (clinica_id, data_inicio, data_fim)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r["event_name"]: {"enviados": r["enviados"], "erros": r["erros"]} for r in rows}


# ============================================================
# PERFIL DEMO (dados fictícios pra apresentação)
# ============================================================
DEMO_PHONE_ID = "DEMO_ESTETICA_AURORA"
DEMO_NOME = "Estética Aurora (Demo)"


def importar_agendamentos(clinica_id, itens):
    """
    Cria agendamentos em lote a partir da importação de planilha. `itens` é uma
    lista de dicts {numero_lead, nome_lead, data_hora(datetime), observacao}.
    Pula duplicado exato (mesmo número + mesma data_hora já confirmado), pra
    reimportar o mesmo arquivo não duplicar. Devolve {criados, duplicados}.
    """
    conn = _conectar()
    cur = conn.cursor()
    criados = 0
    duplicados = 0
    for it in itens:
        cur.execute(
            """SELECT 1 FROM agendamentos
               WHERE clinica_id = %s AND numero_lead = %s AND data_hora = %s
                 AND status = 'confirmado' LIMIT 1""",
            (clinica_id, it["numero_lead"], it["data_hora"])
        )
        if cur.fetchone():
            duplicados += 1
            continue
        cur.execute(
            """INSERT INTO agendamentos
                 (clinica_id, numero_lead, nome_lead, data_hora, duracao_minutos,
                  status, origem, observacao)
               VALUES (%s, %s, %s, %s, %s, 'confirmado', 'importado', %s)""",
            (clinica_id, it["numero_lead"], it.get("nome_lead") or None,
             it["data_hora"], it.get("duracao") or 60, it.get("observacao") or None)
        )
        criados += 1
    conn.commit()
    cur.close()
    conn.close()
    return {"criados": criados, "duplicados": duplicados}


def obter_clinica_demo():
    """Retorna o id da clínica demo, criando-a se não existir. Ela nunca recebe
    webhook real (phone_number_id fake) e fica com CAPI/follow-up desligados."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute("SELECT id FROM clinicas WHERE phone_number_id = %s", (DEMO_PHONE_ID,))
    row = cur.fetchone()
    if row:
        cid = row[0]
    else:
        cur.execute(
            """INSERT INTO clinicas (nome, phone_number_id, system_prompt)
               VALUES (%s, %s, %s) RETURNING id""",
            (DEMO_NOME, DEMO_PHONE_ID, "Clínica demo (apresentação) — não atende de verdade.")
        )
        cid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return cid


def limpar_demo():
    """Apaga todos os dados semeados da clínica demo (mantém a clínica)."""
    cid = obter_clinica_demo()
    conn = _conectar()
    cur = conn.cursor()
    cur.execute("DELETE FROM vendas WHERE clinica_id = %s", (cid,))
    cur.execute("DELETE FROM followups WHERE clinica_id = %s", (cid,))
    cur.execute("DELETE FROM capi_eventos WHERE clinica_id = %s", (cid,))
    cur.execute("DELETE FROM agendamentos WHERE clinica_id = %s", (cid,))
    cur.execute(
        "DELETE FROM mensagens WHERE conversa_id IN (SELECT id FROM conversas WHERE clinica_id = %s)",
        (cid,)
    )
    cur.execute("DELETE FROM conversas WHERE clinica_id = %s", (cid,))
    conn.commit()
    cur.close()
    conn.close()
    return cid


def semear_demo():
    """
    Semeia a clínica demo com dados realistas (escala 'clínica em ascensão'):
    ~70 conversas nos últimos 28 dias espalhadas nas 4 etapas do Kanban,
    ~25 agendamentos e ~10 vendas. Reexecutar limpa e refaz.
    """
    import random
    cid = limpar_demo()
    conn = _conectar()
    cur = conn.cursor()

    nomes = [
        "Juliana Pereira", "Mariana Costa", "Fernanda Lima", "Patrícia Souza",
        "Camila Rocha", "Aline Ferreira", "Bruna Almeida", "Renata Dias",
        "Carla Mendes", "Tatiane Ribeiro", "Vanessa Martins", "Débora Nunes",
        "Sabrina Oliveira", "Larissa Gomes", "Priscila Araújo", "Jéssica Barros",
        "Amanda Teixeira", "Natália Cardoso", "Isabela Freitas", "Michele Santos",
        "Rafaela Moraes", "Gabriela Pinto", "Letícia Ramos", "Bianca Carvalho",
        "Daniela Correia",
    ]
    valores = [786.24, 990.0, 1300.0, 1360.0, 1440.0, 1500.0, 1800.0, 2080.0, 2496.0, 3000.0]
    agora = _agora_brasil()

    def num():
        return ("55" + str(random.choice([51, 11, 54, 35, 47]))
                + "9" + str(random.randint(10000000, 99999999)))

    plano = (["comprou"] * 10 + ["agendado"] * 15
             + ["atendimento"] * 20 + ["novo"] * 25)
    random.shuffle(plano)

    vi = 0
    for i, etapa in enumerate(plano):
        nome = random.choice(nomes)
        numero = num()
        criada = agora - timedelta(days=random.uniform(0, 28), hours=random.uniform(0, 12))
        ctwa = ("democlid_%d" % i) if random.random() < 0.6 else None

        cur.execute(
            """INSERT INTO conversas (clinica_id, numero_lead, criada_em, atualizada_em, ctwa_clid)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (cid, numero, criada, criada, ctwa)
        )
        conv_id = cur.fetchone()[0]

        n_msgs = {"novo": random.randint(1, 2), "atendimento": random.randint(4, 9),
                  "agendado": random.randint(4, 10), "comprou": random.randint(5, 12)}[etapa]
        for k in range(n_msgs):
            role = "user" if k % 2 == 0 else "assistant"
            cur.execute(
                """INSERT INTO mensagens (conversa_id, role, conteudo, criada_em)
                   VALUES (%s, %s, %s, %s)""",
                (conv_id, role, "(mensagem demo)", criada + timedelta(minutes=5 * k))
            )

        if etapa in ("agendado", "comprou"):
            if random.random() < 0.5:
                dh = agora + timedelta(days=random.randint(1, 10), hours=random.randint(0, 6))
            else:
                dh = criada + timedelta(days=random.randint(0, 3), hours=random.randint(1, 6))
            cur.execute(
                """INSERT INTO agendamentos
                     (clinica_id, conversa_id, numero_lead, nome_lead, data_hora,
                      duracao_minutos, status, origem, criado_em)
                   VALUES (%s, %s, %s, %s, %s, 60, 'confirmado', 'ana', %s)""",
                (cid, conv_id, numero, nome, dh, criada + timedelta(hours=1))
            )

        if etapa == "comprou":
            valor = valores[vi % len(valores)]
            vi += 1
            cur.execute(
                """INSERT INTO vendas
                     (clinica_id, conversa_id, valor, moeda, descricao, registrada_em)
                   VALUES (%s, %s, %s, 'BRL', %s, %s)""",
                (cid, conv_id, valor, "Venda demo", criada + timedelta(days=1))
            )

    conn.commit()
    cur.close()
    conn.close()
    return cid


def diagnostico_capi():
    """
    Fotografia do estado do rastreamento pra depuração (admin): config CAPI de
    cada clínica, últimos eventos enviados (com a resposta da Meta) e os últimos
    referrals capturados (com o JSON cru) — inclusive os sem ctwa_clid.
    O token NUNCA é retornado, só se existe ou não.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)

    cur.execute("""
        SELECT id, nome, capi_ativo, meta_dataset_id, meta_page_id,
               (meta_capi_token IS NOT NULL AND meta_capi_token <> '') AS tem_token,
               (meta_test_event_code IS NOT NULL AND meta_test_event_code <> '') AS tem_test_code
        FROM clinicas ORDER BY nome
    """)
    clinicas = cur.fetchall()

    cur.execute("""
        SELECT e.criado_em, e.event_name, e.event_id, e.status,
               LEFT(COALESCE(e.resposta, ''), 400) AS resposta,
               cl.nome AS clinica_nome
        FROM capi_eventos e JOIN clinicas cl ON cl.id = e.clinica_id
        ORDER BY e.criado_em DESC LIMIT 25
    """)
    eventos = cur.fetchall()

    cur.execute("""
        SELECT c.referral_captado_em, c.numero_lead, c.ctwa_clid,
               c.referral_source_type, c.referral_json, cl.nome AS clinica_nome
        FROM conversas c JOIN clinicas cl ON cl.id = c.clinica_id
        WHERE c.referral_captado_em IS NOT NULL
        ORDER BY c.referral_captado_em DESC LIMIT 25
    """)
    referrals = cur.fetchall()

    cur.close()
    conn.close()
    return {"clinicas": clinicas, "eventos": eventos, "referrals": referrals}


def registrar_venda(clinica_id, conversa_id, valor=None, moeda="BRL", descricao=None):
    """Registra uma venda (fecho) numa conversa. Retorna o id da venda."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO vendas (clinica_id, conversa_id, valor, moeda, descricao)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (clinica_id, conversa_id, valor, (moeda or "BRL").strip() or "BRL",
         (descricao or "").strip() or None)
    )
    vid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return vid


def mensagem_ja_processada(message_id_whatsapp):
    """Anti-duplicata: se a Meta reenviar o mesmo webhook, ignoramos."""
    if not message_id_whatsapp:
        return False
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM mensagens WHERE message_id_whatsapp = %s LIMIT 1",
        (message_id_whatsapp,)
    )
    existe = cur.fetchone() is not None
    cur.close()
    conn.close()
    return existe


def salvar_mensagem(conversa_id, role, conteudo, message_id_whatsapp=None):
    """Grava uma mensagem no histórico (do lead ou da Ana)."""
    conn = _conectar()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO mensagens (conversa_id, role, conteudo, message_id_whatsapp)
            VALUES (%s, %s, %s, %s)
            """,
            (conversa_id, role, conteudo, message_id_whatsapp)
        )
        conn.commit()
    except psycopg.errors.UniqueViolation:
        # Mesmo message_id já gravado — duplicata, ignora.
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def obter_historico_conversa(conversa_id, limite=20):
    """
    Pega as últimas N mensagens da conversa, em ordem cronológica.
    No formato que a API do Claude espera.
    """
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, conteudo FROM mensagens
        WHERE conversa_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (conversa_id, limite)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"role": role, "content": conteudo} for role, conteudo in reversed(rows)]


# ============================================================
# FUNÇÕES DO PAINEL
# ============================================================
def buscar_usuario_por_email(email):
    """Retorna o usuário (dict) ou None."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM usuarios WHERE email = %s", (email.lower(),))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user


def conversa_esta_pausada(conversa_id):
    """True se o humano assumiu — Ana não deve responder."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute("SELECT pausada FROM conversas WHERE id = %s", (conversa_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return bool(row and row[0])


def marcar_pausa_conversa(conversa_id, pausada):
    """Pausa (True) ou retoma (False) a Ana naquela conversa."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "UPDATE conversas SET pausada = %s WHERE id = %s",
        (pausada, conversa_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def listar_conversas(clinica_id=None, limite=100):
    """
    Lista conversas pro painel.
    Se clinica_id é None, retorna de TODAS as clínicas (visão admin).
    Cada item já vem com a última mensagem trocada.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    if clinica_id is None:
        cur.execute(
            """
            SELECT c.id, c.numero_lead, c.atualizada_em, c.pausada,
                   cl.nome AS clinica_nome,
                   (SELECT conteudo FROM mensagens
                      WHERE conversa_id = c.id
                      ORDER BY id DESC LIMIT 1) AS ultima_mensagem,
                   (SELECT role FROM mensagens
                      WHERE conversa_id = c.id
                      ORDER BY id DESC LIMIT 1) AS ultima_role
            FROM conversas c
            JOIN clinicas cl ON cl.id = c.clinica_id
            ORDER BY c.atualizada_em DESC
            LIMIT %s
            """,
            (limite,)
        )
    else:
        cur.execute(
            """
            SELECT c.id, c.numero_lead, c.atualizada_em, c.pausada,
                   cl.nome AS clinica_nome,
                   (SELECT conteudo FROM mensagens
                      WHERE conversa_id = c.id
                      ORDER BY id DESC LIMIT 1) AS ultima_mensagem,
                   (SELECT role FROM mensagens
                      WHERE conversa_id = c.id
                      ORDER BY id DESC LIMIT 1) AS ultima_role
            FROM conversas c
            JOIN clinicas cl ON cl.id = c.clinica_id
            WHERE c.clinica_id = %s
            ORDER BY c.atualizada_em DESC
            LIMIT %s
            """,
            (clinica_id, limite)
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def buscar_conversa_completa(conversa_id):
    """Retorna info da conversa + todas as mensagens, em ordem."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT c.id, c.numero_lead, c.pausada, c.clinica_id,
               cl.nome AS clinica_nome, cl.phone_number_id,
               cl.whatsapp_token
        FROM conversas c
        JOIN clinicas cl ON cl.id = c.clinica_id
        WHERE c.id = %s
        """,
        (conversa_id,)
    )
    info = cur.fetchone()
    if not info:
        cur.close()
        conn.close()
        return None

    cur.execute(
        """
        SELECT id, role, conteudo, criada_em
        FROM mensagens
        WHERE conversa_id = %s
        ORDER BY id ASC
        """,
        (conversa_id,)
    )
    msgs = cur.fetchall()
    cur.close()
    conn.close()

    return {"info": info, "mensagens": msgs}


def criar_usuario_clinica(email, senha, nome, clinica_id):
    """Cria um usuário vinculado a uma clínica específica."""
    from werkzeug.security import generate_password_hash
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO usuarios (email, senha_hash, nome, clinica_id)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (email.lower(), generate_password_hash(senha), nome, clinica_id)
    )
    user_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return user_id


def redefinir_senha_usuario(email, nova_senha):
    """Redefine a senha de um usuário pelo email. Retorna o nome do usuário se
    achou e atualizou, ou None se o email não existe."""
    from werkzeug.security import generate_password_hash
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "UPDATE usuarios SET senha_hash = %s WHERE email = %s RETURNING nome",
        (generate_password_hash(nova_senha), email.strip().lower())
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row[0] if row else None


def listar_clinicas():
    """Lista todas as clínicas — pra o admin escolher quando cria usuário."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT id, nome FROM clinicas ORDER BY nome")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def listar_clinicas_com_stats():
    """Lista clínicas com número de conversas e usuários — pra tela admin."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("""
        SELECT
            cl.id, cl.nome, cl.phone_number_id, cl.telefone_humano,
            cl.criada_em,
            (SELECT COUNT(*) FROM conversas WHERE clinica_id = cl.id) AS total_conversas,
            (SELECT COUNT(*) FROM usuarios WHERE clinica_id = cl.id) AS total_usuarios
        FROM clinicas cl
        ORDER BY cl.criada_em DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def obter_clinica(clinica_id):
    """Retorna todos os dados de uma clínica (inclui o prompt)."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM clinicas WHERE id = %s", (clinica_id,))
    clinica = cur.fetchone()
    cur.close()
    conn.close()
    return clinica


def criar_clinica(nome, phone_number_id, system_prompt, telefone_humano, whatsapp_token=None):
    """Cria uma clínica nova. Se whatsapp_token for None, usa o global como fallback."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO clinicas (nome, phone_number_id, system_prompt, telefone_humano, whatsapp_token)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (
            nome.strip(), phone_number_id.strip(), system_prompt,
            (telefone_humano or "").strip(),
            (whatsapp_token or "").strip() or None
        )
    )
    cid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return cid


def atualizar_prompt_clinica(clinica_id, novo_prompt):
    """Atualiza o prompt da Ana de uma clínica específica."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "UPDATE clinicas SET system_prompt = %s WHERE id = %s",
        (novo_prompt, clinica_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def atualizar_token_clinica(clinica_id, novo_token):
    """Atualiza o WhatsApp token de uma clínica específica."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "UPDATE clinicas SET whatsapp_token = %s WHERE id = %s",
        ((novo_token or "").strip() or None, clinica_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def atualizar_clinica(clinica_id, nome=None, phone_number_id=None,
                     telefone_humano=None, whatsapp_token=None,
                     system_prompt=None, meta_dataset_id=None,
                     meta_capi_token=None, capi_ativo=None,
                     meta_test_event_code=None, meta_page_id=None,
                     followup_ativo=None, followup_lembrete_hora=None,
                     followup_template_lembrete=None, followup_template_frio=None,
                     telefones_humanos_extras=None, sala_compartilhada=None,
                     waba_id=None):
    """
    Atualiza apenas os campos passados (não-None) de uma clínica.
    Strings vazias viram None pra telefone/token/campos CAPI (opcionais).
    capi_ativo é boolean (passe True/False pra alterar).
    """
    campos = []
    valores = []

    if nome is not None:
        campos.append("nome = %s")
        valores.append(nome.strip())
    if phone_number_id is not None:
        campos.append("phone_number_id = %s")
        valores.append(phone_number_id.strip())
    if telefone_humano is not None:
        campos.append("telefone_humano = %s")
        valores.append((telefone_humano or "").strip() or None)
    if telefones_humanos_extras is not None:
        campos.append("telefones_humanos_extras = %s")
        valores.append((telefones_humanos_extras or "").strip() or None)
    if sala_compartilhada is not None:
        campos.append("sala_compartilhada = %s")
        valores.append(bool(sala_compartilhada))
    if waba_id is not None:
        campos.append("waba_id = %s")
        valores.append((waba_id or "").strip() or None)
    if whatsapp_token is not None:
        campos.append("whatsapp_token = %s")
        valores.append((whatsapp_token or "").strip() or None)
    if system_prompt is not None:
        campos.append("system_prompt = %s")
        valores.append(system_prompt)
    if meta_dataset_id is not None:
        campos.append("meta_dataset_id = %s")
        valores.append((meta_dataset_id or "").strip() or None)
    if meta_capi_token is not None:
        campos.append("meta_capi_token = %s")
        valores.append((meta_capi_token or "").strip() or None)
    if capi_ativo is not None:
        campos.append("capi_ativo = %s")
        valores.append(bool(capi_ativo))
    if meta_test_event_code is not None:
        campos.append("meta_test_event_code = %s")
        valores.append((meta_test_event_code or "").strip() or None)
    if meta_page_id is not None:
        campos.append("meta_page_id = %s")
        valores.append((meta_page_id or "").strip() or None)
    if followup_ativo is not None:
        campos.append("followup_ativo = %s")
        valores.append(bool(followup_ativo))
    if followup_lembrete_hora is not None:
        campos.append("followup_lembrete_hora = %s")
        valores.append((followup_lembrete_hora or "").strip() or "08:00")
    if followup_template_lembrete is not None:
        campos.append("followup_template_lembrete = %s")
        valores.append((followup_template_lembrete or "").strip() or None)
    if followup_template_frio is not None:
        campos.append("followup_template_frio = %s")
        valores.append((followup_template_frio or "").strip() or None)

    if not campos:
        return

    sql = f"UPDATE clinicas SET {', '.join(campos)} WHERE id = %s"
    valores.append(clinica_id)

    conn = _conectar()
    cur = conn.cursor()
    cur.execute(sql, tuple(valores))
    conn.commit()
    cur.close()
    conn.close()


def buscar_clinica_por_telefone_humano(numero):
    """
    Verifica se um número de WhatsApp pertence ao 'dono' de alguma clínica.
    Compara só os dígitos (ignora formatação).
    Retorna a clínica se encontrar, ou None.
    """
    import re as _re
    digitos = _re.sub(r"\D", "", numero or "")
    if not digitos:
        return None
    # Normaliza variações de DDI: 11999999999, 5511999999999, +5511999999999
    variantes = {digitos}
    if digitos.startswith("55") and len(digitos) > 11:
        variantes.add(digitos[2:])  # sem o 55
    else:
        variantes.add("55" + digitos)  # com 55

    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM clinicas WHERE telefone_humano IS NOT NULL")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    for clinica in rows:
        tel_clinica = _re.sub(r"\D", "", clinica.get("telefone_humano") or "")
        # Compara os últimos 10-11 dígitos (parte sem DDI)
        if tel_clinica:
            if tel_clinica in variantes or any(
                v.endswith(tel_clinica[-10:]) or tel_clinica.endswith(v[-10:])
                for v in variantes if len(v) >= 10
            ):
                return clinica
    return None


# ============================================================
# PROMPT PADRÃO DA CLÍNICA MB (usado só no primeiro seed)
# Se você quiser editar o prompt depois, edita direto no banco.
# ============================================================
PROMPT_MB = """Você é Ana, secretária da MB Odontologia Especializada em Mogi Guaçu/SP.

# QUEM VOCÊ É
Você é uma profissional experiente que entende de gente. Sabe ler nas entrelinhas, captar inseguranças, e perceber a diferença entre um lead curioso e um lead pronto. Você conversa como uma pessoa de verdade conversa: com calma, atenção genuína e sem pressa de vender.

# COMO VOCÊ FALA
- Tom caloroso, porém sóbrio. Acolhedora sem ser animadinha. Você é gentil de um jeito maduro, não exagerado.
- NUNCA use emojis. Nenhum, em hipótese alguma.
- NUNCA comece frases com elogios genéricos ou empolgação artificial ("Ótimo!", "Que legal!", "Entendo!", "Isso faz toda diferença!"). Isso soa como robô fingindo simpatia. Vá direto ao conteúdo, com naturalidade.
- Mensagens curtas: 2 a 4 linhas. Linguagem de pessoa real no WhatsApp, não de atendente de script.

# A SUA POSTURA DE ATENDIMENTO (O MAIS IMPORTANTE)
Seu objetivo NÃO é agendar o mais rápido possível. Seu objetivo é entender profundamente o que o lead quer e fazer uma conversa tão boa que o PRÓPRIO LEAD acabe pedindo a consulta. Você qualifica e desperta interesse; o agendamento é consequência, nunca o foco da sua fala.

REGRA DE OURO: Não ofereça agendamento enquanto o lead não demonstrar interesse claro nele. Se o lead só fez uma pergunta, você responde a pergunta e devolve com OUTRA pergunta que aprofunda o entendimento do caso dele. Deixe o lead falar. Conduza pelo interesse genuíno, não pela oferta repetida de horário.

Só fale em agendar quando: (a) o lead pedir, perguntar como marca, ou perguntar de horário/disponibilidade; ou (b) a conversa amadurecer a ponto de o próximo passo natural ser conhecer a clínica — e mesmo aí, ofereça com leveza, uma vez, sem insistir.

# COMO QUALIFICAR (faça perguntas que revelam o lead)
A cada resposta sua, procure terminar com uma pergunta que te ajude a entender melhor a pessoa. Exemplos do tipo de pergunta que aprofunda:
- "Você já fez alguma avaliação pra saber o que seria ideal pro seu caso, ou seria a primeira vez?" (revela se ele já está pesquisando em outros lugares)
- "O que te incomoda hoje quando você sorri?" (revela a dor real)
- "Você está pensando nisso pra alguma ocasião específica ou é algo que vem te incomodando há um tempo?" (revela urgência)
Essas perguntas mostram interesse real no lead — não em vender. É isso que cria conexão.

# PREÇO: NUNCA dê valor, mas SAIBA lidar com a insistência
Você nunca dá valor, faixa ou estimativa. O valor depende demais de cada caso. MAS: se o lead pergunta preço, não ignore e não repita "depende" de forma fria. Reconheça que é justo querer ter uma noção, explique com honestidade POR QUE varia tanto naquele caso específico, e devolva com uma pergunta de qualificação. O foco sai do número e vai pro contexto do lead. Você só menciona a avaliação presencial como o caminho de ter o valor exato SE isso surgir naturalmente — não como escapatória automática.

# QUANDO O LEAD ESFRIA ("vou ver", "depois eu retorno", "vou pensar")
Isso geralmente significa que ele não viu valor suficiente ou tem uma objeção que não disse. NÃO largue os contatos e desista — isso é fraco. Faça UMA tentativa genuína de reengajar, com uma pergunta leve e real que mostre interesse (ex: descobrir o que ficou faltando, ou o que ele está buscando de fato). UMA vez. Se mesmo assim o lead encerrar, aceite com elegância e tranquilidade, sem perseguir, sem "quando bater a vontade", sem despejar contatos. Premium não corre atrás; mantém a porta aberta com classe.

# INFORMAÇÕES DA CLÍNICA
- Endereço: Rua Mário Vedovello, 72 - Parque São Luiz, Mogi Guaçu/SP
- Horários: Segunda, terça e sábado, 8h às 19h
- Telefone (atendimento humano, se o lead pedir): 19 99343-6676
- Email: odontologiaespecializadamb@gmail.com
- Tratamentos oferecidos:
  - Com o Dr. Matheus: implantes, canal (tratamento endodôntico), pinos intrarradiculares e coroas unitárias.
  - Com a Dra. Maryah: lentes em resina, clareamento dental, restaurações, limpeza (profilaxia), ortodontia (aparelhos tradicionais e invisíveis) e toxina botulínica (Botox).
- Diferenciais: primeira consulta gratuita; atendimento exclusivo e personalizado (não é escala/franquia); resultados naturais; parcelamento em Pix, cartão ou boleto
- Não trabalhamos com convênio, justamente porque cada caso é tratado de forma personalizada

# PROFISSIONAIS E ESPECIALIDADES (IMPORTANTE PARA AGENDAMENTO)
A clínica tem dois profissionais, cada um com sua área e AGENDA PRÓPRIA:
- Dr. Matheus — reabilitação e endodontia (+200 implantes): implantes, canal (tratamento endodôntico), pinos intrarradiculares e coroas unitárias.
- Dra. Maryah — estética e clínica geral (15 anos de experiência, foco em resultado natural): lentes em resina, clareamento dental, restaurações, limpeza (profilaxia), ortodontia (aparelhos tradicionais e invisíveis) e toxina botulínica (Botox).

Na hora de agendar, identifique pelo que o lead procura de quem é o caso e marque na agenda DESSE profissional:
- Implante, canal, pino, coroa, dente que precisa de reconstrução/reabilitação → Dr. Matheus.
- Lente em resina, clareamento, restauração, limpeza, aparelho/alinhador (ortodontia), Botox, estética do sorriso → Dra. Maryah.
Se o lead busca algo que não se encaixa claramente, ou os dois assuntos ao mesmo tempo, pergunte com naturalidade o que ele procura ANTES de verificar horários, pra marcar com o profissional certo. Nunca marque no profissional errado.

# COMO CONSTRUIR VALOR (sem empurrar)
- Diferencie pela experiência e pelo resultado natural, nunca atacando concorrentes.
- Se o lead compara com clínicas mais baratas: traga a diferença entre trabalho padronizado em escala e trabalho personalizado, e o custo de ter que refazer algo malfeito.
- Se o lead tem medo ou insegurança: valide o sentimento com calma e use a experiência dos profissionais como fator de segurança.

Responda sempre como Ana, em no máximo 4 linhas, sem emojis."""


# ============================================================
# PROFISSIONAIS (multi-profissional)
# ============================================================
def listar_profissionais(clinica_id, incluir_inativos=False):
    """Lista os profissionais de uma clínica (só ativos por padrão)."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    sql = "SELECT * FROM profissionais WHERE clinica_id = %s"
    if not incluir_inativos:
        sql += " AND ativo = TRUE"
    sql += " ORDER BY nome ASC"
    cur.execute(sql, (clinica_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def contar_profissionais_ativos(clinica_id):
    """
    Quantos profissionais ativos a clínica tem. 0 = clínica opera no modo
    single (agenda única, comportamento antigo). >= 1 = modo multi.
    """
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM profissionais WHERE clinica_id = %s AND ativo = TRUE",
        (clinica_id,)
    )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


def obter_profissional(profissional_id):
    """Retorna 1 profissional (dict) ou None."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM profissionais WHERE id = %s", (profissional_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def criar_profissional(clinica_id, nome):
    """Cadastra um profissional novo na clínica. Retorna o id."""
    nome = (nome or "").strip()
    if len(nome) < 2:
        raise ValueError("nome do profissional é obrigatório")
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO profissionais (clinica_id, nome)
        VALUES (%s, %s) RETURNING id
        """,
        (clinica_id, nome)
    )
    pid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pid


def atualizar_profissional(profissional_id, nome=None, ativo=None):
    """Atualiza nome e/ou status (ativo) de um profissional. Passar None mantém."""
    campos = []
    valores = []
    if nome is not None:
        n = nome.strip()
        if len(n) < 2:
            raise ValueError("nome do profissional é obrigatório")
        campos.append("nome = %s")
        valores.append(n)
    if ativo is not None:
        campos.append("ativo = %s")
        valores.append(bool(ativo))

    if not campos:
        return True

    sql = f"UPDATE profissionais SET {', '.join(campos)} WHERE id = %s"
    valores.append(profissional_id)
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(sql, tuple(valores))
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


# ============================================================
# SISTEMA DE AGENDA
# ============================================================
from datetime import datetime, timedelta, timezone, time as time_type

TIMEZONE_BRASIL = timezone(timedelta(hours=-3))


def _agora_brasil():
    """Datetime de agora no fuso de Brasília (timezone-aware)."""
    return datetime.now(TIMEZONE_BRASIL)


# ---------- CONFIGURAÇÃO DE HORÁRIOS ----------
def obter_config_horarios(clinica_id, profissional_id=None):
    """
    Retorna a configuração de horários.
    - profissional_id=None: config da clínica (default). Cria com padrões se
      não existir. Comportamento antigo, intacto.
    - profissional_id preenchido: se o profissional tiver override próprio em
      config_horarios_prof, retorna ele; senão, herda a config da clínica.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)

    if profissional_id is not None:
        cur.execute(
            "SELECT * FROM config_horarios_prof WHERE clinica_id = %s AND profissional_id = %s",
            (clinica_id, profissional_id)
        )
        override = cur.fetchone()
        if override:
            cur.close()
            conn.close()
            return override
        # Sem override: cai pra config da clínica (fecha e reabre via chamada normal)

    cur.execute("SELECT * FROM config_horarios WHERE clinica_id = %s", (clinica_id,))
    config = cur.fetchone()
    if not config:
        # Cria com padrões: seg-sex, 9h-18h, 60min, antecedência 3h
        cur.execute(
            """
            INSERT INTO config_horarios (clinica_id) VALUES (%s)
            RETURNING *
            """,
            (clinica_id,)
        )
        config = cur.fetchone()
        conn.commit()
    cur.close()
    conn.close()
    return config


_UNSET = object()  # sentinela: distingue "não mexer" de "limpar (NULL)"


def atualizar_config_horarios(clinica_id, duracao_minutos=None,
                              antecedencia_minima_minutos=None,
                              dias_semana=None, hora_inicio=None,
                              hora_fim=None, almoco_inicio=None,
                              almoco_fim=None, profissional_id=None,
                              hora_inicio_sabado=_UNSET, hora_fim_sabado=_UNSET,
                              hora_inicio_domingo=_UNSET, hora_fim_domingo=_UNSET):
    """
    Atualiza apenas os campos passados; deixa o resto intacto.
    - profissional_id=None: mexe na config da clínica (default). Antigo, intacto.
    - profissional_id preenchido: cria/atualiza o override desse profissional
      em config_horarios_prof (herdando os defaults da clínica no primeiro save).
    """
    campos_valores = [
        ("duracao_minutos", duracao_minutos),
        ("antecedencia_minima_minutos", antecedencia_minima_minutos),
        ("dias_semana", dias_semana),
        ("hora_inicio", hora_inicio),
        ("hora_fim", hora_fim),
        ("almoco_inicio", almoco_inicio),
        ("almoco_fim", almoco_fim),
    ]
    # Fim de semana: None é valor válido (limpa = herda dos úteis), então só
    # entram quando != _UNSET, e podem gravar NULL.
    campos_fds = [
        ("hora_inicio_sabado", hora_inicio_sabado),
        ("hora_fim_sabado", hora_fim_sabado),
        ("hora_inicio_domingo", hora_inicio_domingo),
        ("hora_fim_domingo", hora_fim_domingo),
    ]

    def _acrescenta_fds(campos, valores):
        for nome, valor in campos_fds:
            if valor is not _UNSET:
                campos.append(f"{nome} = %s")
                valores.append(valor)

    if profissional_id is not None:
        # Garante uma linha de override, semeada com a config atual da clínica.
        base = obter_config_horarios(clinica_id)  # default da clínica
        conn = _conectar()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO config_horarios_prof
                (clinica_id, profissional_id, duracao_minutos,
                 antecedencia_minima_minutos, dias_semana, hora_inicio,
                 hora_fim, almoco_inicio, almoco_fim,
                 hora_inicio_sabado, hora_fim_sabado,
                 hora_inicio_domingo, hora_fim_domingo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (clinica_id, profissional_id) DO NOTHING
            """,
            (clinica_id, profissional_id, base["duracao_minutos"],
             base["antecedencia_minima_minutos"], base["dias_semana"],
             base["hora_inicio"], base["hora_fim"],
             base["almoco_inicio"], base["almoco_fim"],
             base.get("hora_inicio_sabado"), base.get("hora_fim_sabado"),
             base.get("hora_inicio_domingo"), base.get("hora_fim_domingo"))
        )
        campos = []
        valores = []
        for nome, valor in campos_valores:
            if valor is not None:
                campos.append(f"{nome} = %s")
                valores.append(valor)
        _acrescenta_fds(campos, valores)
        if campos:
            campos.append("atualizada_em = NOW()")
            sql = (f"UPDATE config_horarios_prof SET {', '.join(campos)} "
                   f"WHERE clinica_id = %s AND profissional_id = %s")
            valores.extend([clinica_id, profissional_id])
            cur.execute(sql, tuple(valores))
        conn.commit()
        cur.close()
        conn.close()
        return

    # Config da clínica (comportamento antigo)
    obter_config_horarios(clinica_id)  # garante que existe

    campos = []
    valores = []
    for nome, valor in campos_valores:
        if valor is not None:
            campos.append(f"{nome} = %s")
            valores.append(valor)
    _acrescenta_fds(campos, valores)

    if not campos:
        return

    campos.append("atualizada_em = NOW()")
    sql = f"UPDATE config_horarios SET {', '.join(campos)} WHERE clinica_id = %s"
    valores.append(clinica_id)

    conn = _conectar()
    cur = conn.cursor()
    cur.execute(sql, tuple(valores))
    conn.commit()
    cur.close()
    conn.close()


# ---------- AGENDAMENTOS ----------
def existe_conflito(clinica_id, data_hora_inicio, duracao_minutos,
                    ignorar_id=None, profissional_id=None):
    """
    Verifica se existe outro agendamento confirmado OU bloqueio que se sobrepõe
    ao intervalo [data_hora_inicio, data_hora_inicio + duracao_minutos].
    Retorna True se há conflito, False se está livre.

    - profissional_id=None: checa a clínica inteira (comportamento single antigo).
    - profissional_id preenchido (modo multi): só conflita com agendamentos DESSE
      profissional; bloqueios conflitam se forem do profissional OU da clínica
      toda (profissional_id NULL = feriado/ausência geral).
    """
    fim = data_hora_inicio + timedelta(minutes=duracao_minutos)

    conn = _conectar()
    cur = conn.cursor()

    # Sala compartilhada: se ligado, QUALQUER agendamento ocupa a sala e bloqueia
    # todos os profissionais (recurso físico único). Bloqueios continuam PESSOAIS.
    cur.execute(
        "SELECT COALESCE(sala_compartilhada, FALSE) FROM clinicas WHERE id = %s",
        (clinica_id,)
    )
    row_sc = cur.fetchone()
    sala_compartilhada = bool(row_sc[0]) if row_sc else False

    # 1) Conflito com outros agendamentos
    sql_ag = """
        SELECT 1 FROM agendamentos
        WHERE clinica_id = %s
          AND status = 'confirmado'
          AND data_hora < %s
          AND data_hora + (duracao_minutos || ' minutes')::interval > %s
    """
    params = [clinica_id, fim, data_hora_inicio]
    if profissional_id is not None and not sala_compartilhada:
        # Modo salas separadas: conflita só com a agenda desse profissional E com
        # agendamentos sem profissional (legado/manual = "cadeira ocupada"). Se a
        # sala é compartilhada, não filtra: qualquer agendamento ocupa a sala.
        sql_ag += " AND (profissional_id = %s OR profissional_id IS NULL)"
        params.append(profissional_id)
    if ignorar_id is not None:
        sql_ag += " AND id <> %s"
        params.append(ignorar_id)
    sql_ag += " LIMIT 1"
    cur.execute(sql_ag, tuple(params))
    if cur.fetchone():
        cur.close()
        conn.close()
        return True

    # 2) Conflito com bloqueios
    sql_bl = """
        SELECT 1 FROM bloqueios
        WHERE clinica_id = %s
          AND inicio < %s
          AND fim > %s
    """
    params_bl = [clinica_id, fim, data_hora_inicio]
    if profissional_id is not None:
        # Bloqueio do profissional OU da clínica toda (NULL)
        sql_bl += " AND (profissional_id = %s OR profissional_id IS NULL)"
        params_bl.append(profissional_id)
    sql_bl += " LIMIT 1"
    cur.execute(sql_bl, tuple(params_bl))
    conflito = cur.fetchone() is not None
    cur.close()
    conn.close()
    return conflito


def criar_agendamento(clinica_id, numero_lead, data_hora, duracao_minutos=None,
                      nome_lead=None, conversa_id=None, origem='manual',
                      observacao=None, profissional_id=None):
    """
    Cria um agendamento, validando anti-conflito.
    Retorna o id do agendamento ou levanta ValueError se conflitar.
    Se profissional_id for passado, usa a config de horários e o anti-conflito
    daquele profissional.
    """
    if duracao_minutos is None:
        config = obter_config_horarios(clinica_id, profissional_id)
        duracao_minutos = config["duracao_minutos"]

    if existe_conflito(clinica_id, data_hora, duracao_minutos,
                       profissional_id=profissional_id):
        raise ValueError("horário ocupado")

    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agendamentos
            (clinica_id, conversa_id, numero_lead, nome_lead,
             data_hora, duracao_minutos, status, origem, observacao,
             profissional_id)
        VALUES (%s, %s, %s, %s, %s, %s, 'confirmado', %s, %s, %s)
        RETURNING id
        """,
        (clinica_id, conversa_id, numero_lead, nome_lead,
         data_hora, duracao_minutos, origem, observacao, profissional_id)
    )
    ag_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return ag_id


def cancelar_agendamento(agendamento_id):
    """Marca um agendamento como cancelado (não apaga, só muda o status)."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE agendamentos
        SET status = 'cancelado', cancelado_em = NOW()
        WHERE id = %s AND status = 'confirmado'
        """,
        (agendamento_id,)
    )
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def remarcar_agendamento(agendamento_id, nova_data_hora):
    """Move um agendamento pra outro horário, validando anti-conflito.
    Respeita o profissional do agendamento (só conflita com a agenda dele)."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT clinica_id, duracao_minutos, profissional_id FROM agendamentos WHERE id = %s",
        (agendamento_id,)
    )
    ag = cur.fetchone()
    if not ag:
        cur.close()
        conn.close()
        raise ValueError("agendamento não encontrado")

    if existe_conflito(ag["clinica_id"], nova_data_hora,
                       ag["duracao_minutos"], ignorar_id=agendamento_id,
                       profissional_id=ag["profissional_id"]):
        cur.close()
        conn.close()
        raise ValueError("horário ocupado")

    cur.execute(
        "UPDATE agendamentos SET data_hora = %s WHERE id = %s",
        (nova_data_hora, agendamento_id)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True


def atualizar_agendamento(agendamento_id, nome_lead=None, observacao=None,
                          data_hora=None, profissional_id=None):
    """
    Atualiza campos editáveis de um agendamento. Passar None mantém o valor atual.

    NÃO valida anti-conflito — quem chama é responsável por checar (ex: o endpoint
    de edição valida a agenda do profissional de destino antes de trocar). Pra
    mudança simples de horário (mesmo profissional) prefira remarcar_agendamento,
    que já valida. Retorna True se o agendamento existia.
    """
    campos = []
    valores = []
    if nome_lead is not None:
        campos.append("nome_lead = %s")
        valores.append(nome_lead.strip())
    if observacao is not None:
        campos.append("observacao = %s")
        valores.append((observacao or "").strip() or None)
    if data_hora is not None:
        campos.append("data_hora = %s")
        valores.append(data_hora)
    if profissional_id is not None:
        campos.append("profissional_id = %s")
        valores.append(profissional_id)

    if not campos:
        return True

    sql = f"UPDATE agendamentos SET {', '.join(campos)} WHERE id = %s"
    valores.append(agendamento_id)

    conn = _conectar()
    cur = conn.cursor()
    cur.execute(sql, tuple(valores))
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def listar_agendamentos(clinica_id, data_inicio, data_fim,
                        incluir_cancelados=False, profissional_id=None):
    """
    Lista agendamentos da clínica num intervalo de datas.
    data_inicio e data_fim são datetimes (timezone-aware).
    Se profissional_id for passado, filtra só os daquele profissional.
    Cada linha inclui profissional_id e profissional_nome (NULL se sem profissional).
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    sql_status = "" if incluir_cancelados else "AND a.status = 'confirmado'"
    params = [clinica_id, data_inicio, data_fim]
    sql_prof = ""
    if profissional_id is not None:
        sql_prof = "AND a.profissional_id = %s"
        params.append(profissional_id)
    cur.execute(
        f"""
        SELECT a.id, a.clinica_id, a.conversa_id, a.numero_lead, a.nome_lead,
               a.data_hora, a.duracao_minutos, a.status, a.origem, a.observacao,
               a.criado_em, a.confirmacao_24h_enviada, a.profissional_id,
               p.nome AS profissional_nome
        FROM agendamentos a
        LEFT JOIN profissionais p ON p.id = a.profissional_id
        WHERE a.clinica_id = %s
          AND a.data_hora >= %s AND a.data_hora < %s
          {sql_status}
          {sql_prof}
        ORDER BY a.data_hora ASC
        """,
        tuple(params)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def obter_agendamento(agendamento_id):
    """Retorna dados completos de 1 agendamento."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM agendamentos WHERE id = %s", (agendamento_id,))
    ag = cur.fetchone()
    cur.close()
    conn.close()
    return ag


def obter_nome_lead(clinica_id, numero_lead):
    """
    Retorna o nome mais recente já registrado pra esse contato (de qualquer
    agendamento — ativo, cancelado ou passado), ou None. Serve pra Ana não
    perguntar o nome de novo quando o histórico de mensagens já saiu da janela.
    """
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT nome_lead FROM agendamentos
        WHERE clinica_id = %s AND numero_lead = %s
          AND nome_lead IS NOT NULL AND TRIM(nome_lead) <> ''
        ORDER BY criado_em DESC
        LIMIT 1
        """,
        (clinica_id, numero_lead)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def buscar_agendamentos_ativos_lead(clinica_id, numero_lead):
    """Retorna os agendamentos confirmados (futuros) de um lead específico nessa clínica."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT id, data_hora, nome_lead, observacao
        FROM agendamentos
        WHERE clinica_id = %s AND numero_lead = %s AND status = 'confirmado'
        ORDER BY data_hora ASC
        """,
        (clinica_id, numero_lead)
    )
    ags = cur.fetchall()
    cur.close()
    conn.close()
    return ags


# ---------- HORÁRIOS DISPONÍVEIS ----------
def obter_horarios_disponiveis(clinica_id, data, profissional_id=None):
    """
    Calcula a lista de horários livres pra agendamento numa data específica.
    Retorna lista de datetimes (timezone-aware) que estão disponíveis.

    Se profissional_id for passado, usa a config e a ocupação DAQUELE profissional
    (horário ocupado de outro profissional não bloqueia este).

    Considera:
    - Configuração de dias da semana atendidos
    - Janela de horário (início/fim do expediente)
    - Almoço (se configurado)
    - Duração da consulta (slots de N em N minutos)
    - Agendamentos já marcados
    - Bloqueios manuais
    - Antecedência mínima (não permite marcar muito perto)
    """
    config = obter_config_horarios(clinica_id, profissional_id)
    dias_atendidos = [int(d) for d in config["dias_semana"].split(",")]

    # weekday(): segunda = 0, ... domingo = 6
    # mas o config usa 1 = seg, ... 7 = dom (jeito mais natural)
    if (data.weekday() + 1) not in dias_atendidos:
        return []

    duracao = config["duracao_minutos"]
    # Janela do dia: dias úteis (seg-sex) usam hora_inicio/hora_fim; sábado e
    # domingo podem ter horário próprio. Sem horário próprio, herdam o dos úteis.
    wd = data.weekday()  # seg=0 ... sáb=5, dom=6
    if wd == 5 and config.get("hora_inicio_sabado") and config.get("hora_fim_sabado"):
        hora_ini = config["hora_inicio_sabado"]
        hora_fim = config["hora_fim_sabado"]
    elif wd == 6 and config.get("hora_inicio_domingo") and config.get("hora_fim_domingo"):
        hora_ini = config["hora_inicio_domingo"]
        hora_fim = config["hora_fim_domingo"]
    else:
        hora_ini = config["hora_inicio"]
        hora_fim = config["hora_fim"]
    almoco_ini = config["almoco_inicio"]
    almoco_fim = config["almoco_fim"]

    # Monta os limites do dia em datetime com timezone Brasil
    inicio_dia = datetime.combine(data, hora_ini).replace(tzinfo=TIMEZONE_BRASIL)
    fim_dia = datetime.combine(data, hora_fim).replace(tzinfo=TIMEZONE_BRASIL)

    # Janela proibida do almoço (se houver)
    almoco_inicio_dt = None
    almoco_fim_dt = None
    if almoco_ini and almoco_fim:
        almoco_inicio_dt = datetime.combine(data, almoco_ini).replace(tzinfo=TIMEZONE_BRASIL)
        almoco_fim_dt = datetime.combine(data, almoco_fim).replace(tzinfo=TIMEZONE_BRASIL)

    # Antecedência mínima: não pode marcar antes de "agora + X minutos"
    agora = _agora_brasil()
    primeiro_horario_valido = agora + timedelta(
        minutes=config["antecedencia_minima_minutos"]
    )

    # Busca agendamentos do dia em uma query.
    conn = _conectar()
    cur = conn.cursor()

    # Sala compartilhada: agendamento de qualquer profissional ocupa a sala.
    cur.execute(
        "SELECT COALESCE(sala_compartilhada, FALSE) FROM clinicas WHERE id = %s",
        (clinica_id,)
    )
    row_sc = cur.fetchone()
    sala_compartilhada = bool(row_sc[0]) if row_sc else False

    sql_ocup = """
        SELECT data_hora, duracao_minutos FROM agendamentos
        WHERE clinica_id = %s
          AND status = 'confirmado'
          AND data_hora >= %s AND data_hora < %s
    """
    params_ocup = [clinica_id, inicio_dia - timedelta(hours=4),
                   fim_dia + timedelta(hours=4)]
    if profissional_id is not None and not sala_compartilhada:
        # Salas separadas: só a agenda desse profissional + agendamentos sem
        # profissional. Sala compartilhada: não filtra (qualquer um ocupa a sala).
        sql_ocup += " AND (profissional_id = %s OR profissional_id IS NULL)"
        params_ocup.append(profissional_id)
    cur.execute(sql_ocup, tuple(params_ocup))
    ocupados = [
        (linha[0], linha[0] + timedelta(minutes=linha[1]))
        for linha in cur.fetchall()
    ]

    # Busca bloqueios que se sobrepõem ao dia.
    # Modo multi: bloqueio do profissional OU da clínica toda (NULL).
    sql_bloq = """
        SELECT inicio, fim FROM bloqueios
        WHERE clinica_id = %s
          AND inicio < %s AND fim > %s
    """
    params_bloq = [clinica_id, fim_dia, inicio_dia]
    if profissional_id is not None:
        sql_bloq += " AND (profissional_id = %s OR profissional_id IS NULL)"
        params_bloq.append(profissional_id)
    cur.execute(sql_bloq, tuple(params_bloq))
    bloqueios = [(linha[0], linha[1]) for linha in cur.fetchall()]
    cur.close()
    conn.close()

    # Gera slots de duração em duração e filtra
    slots_livres = []
    slot = inicio_dia
    while slot + timedelta(minutes=duracao) <= fim_dia:
        slot_fim = slot + timedelta(minutes=duracao)

        # Filtros:
        ok = True
        # 1) Antecedência mínima
        if slot < primeiro_horario_valido:
            ok = False
        # 2) Almoço
        if ok and almoco_inicio_dt and slot < almoco_fim_dt and slot_fim > almoco_inicio_dt:
            ok = False
        # 3) Agendamentos existentes
        if ok:
            for ag_ini, ag_fim in ocupados:
                if slot < ag_fim and slot_fim > ag_ini:
                    ok = False
                    break
        # 4) Bloqueios
        if ok:
            for b_ini, b_fim in bloqueios:
                if slot < b_fim and slot_fim > b_ini:
                    ok = False
                    break

        if ok:
            slots_livres.append(slot)
        slot += timedelta(minutes=duracao)

    return slots_livres


def obter_horarios_disponiveis_intervalo(clinica_id, data_inicio, data_fim,
                                          max_por_dia=None, profissional_id=None):
    """
    Retorna TODOS os horários disponíveis num intervalo de datas.
    Se max_por_dia for definido, limita a quantidade por dia (espaçando).
    Se profissional_id for passado, calcula pela agenda desse profissional.
    Padrão: retorna tudo, deixa a Ana decidir o que mostrar.
    """
    resultado = []
    dia = data_inicio
    while dia <= data_fim:
        slots = obter_horarios_disponiveis(clinica_id, dia, profissional_id)
        if not slots:
            dia = dia + timedelta(days=1)
            continue
        if max_por_dia is None or len(slots) <= max_por_dia:
            resultado.extend(slots)
        else:
            # Se foi pedido limitar, espaça mantendo primeiro e último
            indices = [0, len(slots) - 1]
            if max_por_dia > 2:
                passo = (len(slots) - 1) / (max_por_dia - 1)
                for i in range(1, max_por_dia - 1):
                    idx = round(i * passo)
                    if idx not in indices:
                        indices.append(idx)
            indices = sorted(set(indices))[:max_por_dia]
            resultado.extend([slots[i] for i in indices])
        dia = dia + timedelta(days=1)
    return resultado


# ---------- BLOQUEIOS ----------
def criar_bloqueio(clinica_id, inicio, fim, motivo=None, profissional_id=None):
    """Cria um bloqueio (período sem atendimento).
    profissional_id=None: vale pra clínica toda. Preenchido: só aquele profissional."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bloqueios (clinica_id, inicio, fim, motivo, profissional_id)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (clinica_id, inicio, fim, motivo, profissional_id)
    )
    bid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return bid


def remover_bloqueio(bloqueio_id):
    """Apaga um bloqueio. Retorna True se existia, False se não."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute("DELETE FROM bloqueios WHERE id = %s", (bloqueio_id,))
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def listar_bloqueios(clinica_id, data_inicio, data_fim, profissional_id=None):
    """Lista bloqueios num intervalo de datas.
    Modo multi (profissional_id): traz os do profissional + os da clínica toda (NULL).
    Cada linha inclui profissional_id e profissional_nome."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    params = [clinica_id, data_fim, data_inicio]
    sql_prof = ""
    if profissional_id is not None:
        sql_prof = "AND (b.profissional_id = %s OR b.profissional_id IS NULL)"
        params.append(profissional_id)
    cur.execute(
        f"""
        SELECT b.id, b.inicio, b.fim, b.motivo, b.criado_em, b.profissional_id,
               p.nome AS profissional_nome
        FROM bloqueios b
        LEFT JOIN profissionais p ON p.id = b.profissional_id
        WHERE b.clinica_id = %s
          AND b.inicio < %s AND b.fim > %s
          {sql_prof}
        ORDER BY b.inicio ASC
        """,
        tuple(params)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
