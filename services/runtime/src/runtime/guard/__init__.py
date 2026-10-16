"""工具闸口（Guard）子包：实现工具调用的准入判定，固定顺序为
hooks → deny → policy → risk → approval → idempotency → sandbox。"""
