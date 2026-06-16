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
