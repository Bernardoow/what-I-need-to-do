# Compartilhar o núcleo entre o CLI e a futura janela

As operações de tarefa ficam em um módulo Python que cada interface pode chamar diretamente. O módulo usa uma interface de armazenamento, com o Notion como primeira implementação. Assim o CLI atende ao uso inicial e a futura janela pode aproveitar as mesmas operações de tarefa.

Em um terminal interativo, `show`, `update`, `done` e `delete` abrem um seletor com busca por título quando chamados sem ID. Após a seleção, `update` aplica as opções informadas ou mostra as ações disponíveis. Quando recebem um ID, os comandos atuam diretamente sobre a tarefa.
