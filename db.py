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

    # Índice pra busca rápida de duplicatas.
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_mensagens_msg_id
        ON mensagens(message_id_whatsapp);
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
- Especialidades: Lentes de Resina e Facetas (Dra. Maryah, 15 anos de experiência, foco em resultado natural), Alinhadores Invisíveis Esthetic Aligner, Implantes (Dr. Matheus, +200 casos), Ortodontia
- Diferenciais: primeira consulta gratuita; atendimento exclusivo e personalizado (não é escala/franquia); resultados naturais; parcelamento em Pix, cartão ou boleto
- Não trabalhamos com convênio, justamente porque cada caso é tratado de forma personalizada

# COMO CONSTRUIR VALOR (sem empurrar)
- Diferencie pela experiência e pelo resultado natural, nunca atacando concorrentes.
- Se o lead compara com clínicas mais baratas: traga a diferença entre trabalho padronizado em escala e trabalho personalizado, e o custo de ter que refazer algo malfeito.
- Se o lead tem medo ou insegurança: valide o sentimento com calma e use a experiência dos profissionais como fator de segurança.

Responda sempre como Ana, em no máximo 4 linhas, sem emojis."""


# ============================================================
# SISTEMA DE AGENDA
# ============================================================
from datetime import datetime, timedelta, timezone, time as time_type

TIMEZONE_BRASIL = timezone(timedelta(hours=-3))


def _agora_brasil():
    """Datetime de agora no fuso de Brasília (timezone-aware)."""
    return datetime.now(TIMEZONE_BRASIL)


# ---------- CONFIGURAÇÃO DE HORÁRIOS ----------
def obter_config_horarios(clinica_id):
    """
    Retorna a configuração de horários da clínica.
    Se ainda não existir, cria com padrões e retorna.
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
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


def atualizar_config_horarios(clinica_id, duracao_minutos=None,
                              antecedencia_minima_minutos=None,
                              dias_semana=None, hora_inicio=None,
                              hora_fim=None, almoco_inicio=None,
                              almoco_fim=None):
    """Atualiza apenas os campos passados; deixa o resto intacto."""
    # Garante que a config existe
    obter_config_horarios(clinica_id)

    campos = []
    valores = []
    for nome, valor in [
        ("duracao_minutos", duracao_minutos),
        ("antecedencia_minima_minutos", antecedencia_minima_minutos),
        ("dias_semana", dias_semana),
        ("hora_inicio", hora_inicio),
        ("hora_fim", hora_fim),
        ("almoco_inicio", almoco_inicio),
        ("almoco_fim", almoco_fim),
    ]:
        if valor is not None:
            campos.append(f"{nome} = %s")
            valores.append(valor)

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
                    ignorar_id=None):
    """
    Verifica se existe outro agendamento confirmado OU bloqueio que se sobrepõe
    ao intervalo [data_hora_inicio, data_hora_inicio + duracao_minutos].
    Retorna True se há conflito, False se está livre.
    """
    fim = data_hora_inicio + timedelta(minutes=duracao_minutos)

    conn = _conectar()
    cur = conn.cursor()

    # 1) Conflito com outros agendamentos
    sql_ag = """
        SELECT 1 FROM agendamentos
        WHERE clinica_id = %s
          AND status = 'confirmado'
          AND data_hora < %s
          AND data_hora + (duracao_minutos || ' minutes')::interval > %s
    """
    params = [clinica_id, fim, data_hora_inicio]
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
    cur.execute(
        """
        SELECT 1 FROM bloqueios
        WHERE clinica_id = %s
          AND inicio < %s
          AND fim > %s
        LIMIT 1
        """,
        (clinica_id, fim, data_hora_inicio)
    )
    conflito = cur.fetchone() is not None
    cur.close()
    conn.close()
    return conflito


def criar_agendamento(clinica_id, numero_lead, data_hora, duracao_minutos=None,
                      nome_lead=None, conversa_id=None, origem='manual',
                      observacao=None):
    """
    Cria um agendamento, validando anti-conflito.
    Retorna o id do agendamento ou levanta ValueError se conflitar.
    """
    if duracao_minutos is None:
        config = obter_config_horarios(clinica_id)
        duracao_minutos = config["duracao_minutos"]

    if existe_conflito(clinica_id, data_hora, duracao_minutos):
        raise ValueError("horário ocupado")

    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agendamentos
            (clinica_id, conversa_id, numero_lead, nome_lead,
             data_hora, duracao_minutos, status, origem, observacao)
        VALUES (%s, %s, %s, %s, %s, %s, 'confirmado', %s, %s)
        RETURNING id
        """,
        (clinica_id, conversa_id, numero_lead, nome_lead,
         data_hora, duracao_minutos, origem, observacao)
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
    """Move um agendamento pra outro horário, validando anti-conflito."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT clinica_id, duracao_minutos FROM agendamentos WHERE id = %s",
        (agendamento_id,)
    )
    ag = cur.fetchone()
    if not ag:
        cur.close()
        conn.close()
        raise ValueError("agendamento não encontrado")

    if existe_conflito(ag["clinica_id"], nova_data_hora,
                       ag["duracao_minutos"], ignorar_id=agendamento_id):
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


def listar_agendamentos(clinica_id, data_inicio, data_fim,
                        incluir_cancelados=False):
    """
    Lista agendamentos da clínica num intervalo de datas.
    data_inicio e data_fim são datetimes (timezone-aware).
    """
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    if incluir_cancelados:
        sql_status = ""
    else:
        sql_status = "AND status = 'confirmado'"
    cur.execute(
        f"""
        SELECT id, clinica_id, conversa_id, numero_lead, nome_lead,
               data_hora, duracao_minutos, status, origem, observacao,
               criado_em, confirmacao_24h_enviada
        FROM agendamentos
        WHERE clinica_id = %s
          AND data_hora >= %s AND data_hora < %s
          {sql_status}
        ORDER BY data_hora ASC
        """,
        (clinica_id, data_inicio, data_fim)
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


# ---------- HORÁRIOS DISPONÍVEIS ----------
def obter_horarios_disponiveis(clinica_id, data):
    """
    Calcula a lista de horários livres pra agendamento numa data específica.
    Retorna lista de datetimes (timezone-aware) que estão disponíveis.

    Considera:
    - Configuração de dias da semana atendidos
    - Janela de horário (início/fim do expediente)
    - Almoço (se configurado)
    - Duração da consulta (slots de N em N minutos)
    - Agendamentos já marcados
    - Bloqueios manuais
    - Antecedência mínima (não permite marcar muito perto)
    """
    config = obter_config_horarios(clinica_id)
    dias_atendidos = [int(d) for d in config["dias_semana"].split(",")]

    # weekday(): segunda = 0, ... domingo = 6
    # mas o config usa 1 = seg, ... 7 = dom (jeito mais natural)
    if (data.weekday() + 1) not in dias_atendidos:
        return []

    duracao = config["duracao_minutos"]
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

    # Busca agendamentos do dia em uma query
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT data_hora, duracao_minutos FROM agendamentos
        WHERE clinica_id = %s
          AND status = 'confirmado'
          AND data_hora >= %s AND data_hora < %s
        """,
        (clinica_id, inicio_dia - timedelta(hours=4), fim_dia + timedelta(hours=4))
    )
    ocupados = [
        (linha[0], linha[0] + timedelta(minutes=linha[1]))
        for linha in cur.fetchall()
    ]

    # Busca bloqueios que se sobrepõem ao dia
    cur.execute(
        """
        SELECT inicio, fim FROM bloqueios
        WHERE clinica_id = %s
          AND inicio < %s AND fim > %s
        """,
        (clinica_id, fim_dia, inicio_dia)
    )
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
                                          max_por_dia=4):
    """
    Variante: retorna até N horários por dia num intervalo de datas.
    Útil pra Ana propor opções pro lead ("tenho sexta às 10h, segunda às 14h...").
    """
    resultado = []
    dia = data_inicio
    while dia <= data_fim:
        slots = obter_horarios_disponiveis(clinica_id, dia)
        # Pega N espaçados ao longo do dia (não os primeiros — varia mais)
        if slots:
            if len(slots) <= max_por_dia:
                resultado.extend(slots)
            else:
                # Pega espaçado: 1º, último e intermediários
                passo = max(1, len(slots) // max_por_dia)
                resultado.extend(slots[::passo][:max_por_dia])
        dia = dia + timedelta(days=1)
    return resultado


# ---------- BLOQUEIOS ----------
def criar_bloqueio(clinica_id, inicio, fim, motivo=None):
    """Cria um bloqueio (período em que a clínica não atende)."""
    conn = _conectar()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bloqueios (clinica_id, inicio, fim, motivo)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (clinica_id, inicio, fim, motivo)
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


def listar_bloqueios(clinica_id, data_inicio, data_fim):
    """Lista bloqueios num intervalo de datas."""
    conn = _conectar()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT id, inicio, fim, motivo, criado_em FROM bloqueios
        WHERE clinica_id = %s
          AND inicio < %s AND fim > %s
        ORDER BY inicio ASC
        """,
        (clinica_id, data_fim, data_inicio)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
