---
name: crmtg-ops-and-sync
description: >-
  Use when working in the crmtg repository (Telegram CRM, folder synchronization, Web
  Telegram session on server, MCP server for agent analytics, Airtable lead sync, and
  Contabo VPS production deployment).
---

# Telegram CRM (`crmtg`) — Folders, Server Session, MCP & Airtable Skill

## 1. Синхронизация реальных папок Telegram (Dialog Filters)
- Папки с клиентами в CRM соответствуют **реальным папкам (Dialog Filters) в Telegram**.
- Поддерживай синхронизацию папок **как по одной выбранной папке, так и по нескольким сразу**.
- **Защита от мусорного импорта**: при проверке синхронизации папок **не выкачивай автоматически все чаты из личных папок владельца в таблицу лидов** и не заливай демо-базу («300 тестовых лидов») на прод. В базе прода должны лежать только реальные целевые лиды/папки.

## 2. Серверный рантайм Web Telegram и деплой на VPS Contabo (169.58.145.85)
- Web Telegram и клиентская сессия должны работать **на сервере VPS Contabo (`/opt/crmtg`, `crmtg.service`)**, а не опираться на локальный процесс или локальную БД Мака.
- Никогда не отдавай для командного тестирования временную публичную ссылку (туннель) на локальную базу — выкатывай изменения через GitHub Actions (`deploy-vps.yml`) на VPS (`https://crm.softugc.com` / `https://crm-169-58-145-85.sslip.io`) и проверяй, что Web Telegram и API открываются на серверном URL без ошибок.

## 3. MCP-сервер и интеграция с Airtable
- Поддерживай MCP-интерфейс проекта, чтобы внешние агенты могли запрашивать сквозную статистику по клиентам, папкам и конверсиям.
- Обеспечивай чистую синхронизацию целевых лидов и статусов с Airtable без дублей.
