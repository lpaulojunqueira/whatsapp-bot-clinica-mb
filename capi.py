"""
Módulo da Conversions API (CAPI) da Meta.

Envia eventos de conversão (LeadStarted, Lead, Schedule) pro dataset do TENANT
da conversa, atribuindo aos anúncios click-to-WhatsApp via ctwa_clid.

Princípios:
- Multi-tenant: usa dataset_id e token DA CLÍNICA da conversa.
- Desligado por padrão: sem capi_ativo, sem credenciais ou sem ctwa_clid,
  não envia nada e não gera warning repetitivo.
- Nunca quebra o atendimento: qualquer falha é logada e o fluxo segue.
- Deduplicação: event_id determinístico + registro em capi_eventos.
"""

import time
import requests

from db import capi_evento_ja_enviado, registrar_evento_capi

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


def _capi_configurado(clinica):
    """True se a clínica está com CAPI ativo e com dataset + token preenchidos."""
    return bool(
        clinica.get("capi_ativo")
        and (clinica.get("meta_dataset_id") or "").strip()
        and (clinica.get("meta_capi_token") or "").strip()
    )


def enviar_evento(clinica, conversa, event_name, event_id,
                  extra_user_data=None, custom_data=None):
    """
    Envia um evento pra Conversions API do tenant. Retorna True se enviou,
    False se pulou ou falhou (sem levantar exceção).

    - clinica: dict da clínica (tem capi_ativo, meta_dataset_id, meta_capi_token,
      meta_test_event_code).
    - conversa: dict da conversa (tem ctwa_clid).
    - event_name: "Contact", "Lead", "Schedule", "Purchase", etc.
    - event_id: id determinístico pra deduplicação (ex: "schedule:123").
    - custom_data: dict opcional (ex: {"value": 3000.0, "currency": "BRL"} no Purchase).
    """
    try:
        # 1) Rastreamento desligado pra esse tenant — sai quieto, sem warning.
        if not _capi_configurado(clinica):
            return False

        # 2) Sem ctwa_clid não há como atribuir ao anúncio — pula.
        ctwa_clid = (conversa or {}).get("ctwa_clid")
        if not ctwa_clid:
            return False

        # 3) Já enviado com sucesso antes — deduplicação.
        if capi_evento_ja_enviado(event_id):
            return False

        dataset_id = clinica["meta_dataset_id"].strip()
        token = clinica["meta_capi_token"].strip()

        user_data = {"ctwa_clid": ctwa_clid}
        if extra_user_data:
            user_data.update(extra_user_data)

        evento = {
            "event_name": event_name,
            "event_time": int(time.time()),
            "action_source": "business_messaging",
            "messaging_channel": "whatsapp",
            "event_id": event_id,
            "user_data": user_data,
            # Exigido pelos exemplos oficiais da CAPI de mensagens — sem isso a
            # Meta aceita o POST mas não contabiliza o evento no dataset.
            "messaging_outcome_data": {"outcome_type": "automatic_events"},
        }
        if custom_data:
            evento["custom_data"] = custom_data
        payload = {"data": [evento]}

        # Test event code (opcional): faz o evento aparecer na aba "Test Events"
        # do Gerenciador de Eventos, sem contar como conversão de produção.
        test_code = (clinica.get("meta_test_event_code") or "").strip()
        if test_code:
            payload["test_event_code"] = test_code

        url = f"{GRAPH_API_URL}/{dataset_id}/events"
        r = requests.post(
            url, params={"access_token": token}, json=payload, timeout=15
        )

        if r.status_code == 200:
            registrar_evento_capi(
                clinica["id"], (conversa or {}).get("id"),
                event_name, event_id, "enviado", r.text
            )
            print(f"📊 CAPI {event_name} enviado (event_id={event_id}) "
                  f"clinica={clinica.get('nome')}")
            return True

        # Meta recusou — loga o motivo pra depurar, mas não quebra o fluxo.
        registrar_evento_capi(
            clinica["id"], (conversa or {}).get("id"),
            event_name, event_id, "erro", f"HTTP {r.status_code}: {r.text}"
        )
        print(f"❌ CAPI {event_name} recusado ({r.status_code}): {r.text[:300]}")
        return False

    except Exception as e:
        # Falha de rede/exceção — registra e segue. Atendimento nunca para por CAPI.
        try:
            registrar_evento_capi(
                clinica.get("id"), (conversa or {}).get("id"),
                event_name, event_id, "erro", f"excecao: {e}"
            )
        except Exception:
            pass
        print(f"❌ CAPI {event_name} exceção: {e}")
        return False
