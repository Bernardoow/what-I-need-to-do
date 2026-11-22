# win-td

Capture e revise tarefas pessoais no terminal, com uma lista do Notion por destino.

## Instalação

Use Python 3.11 ou posterior. No diretório do projeto, rode `uv tool install .` para instalar `win-td` no seu usuário. Com o diretório de executáveis do uv no `PATH`, rode `win-td --help` de qualquer diretório.

Em um ambiente virtual ativo, você também pode instalar com `python -m pip install .`. Para rodar direto do diretório do projeto, use `python -m todo_cli --help`.

## Configuração

Crie uma conexão interna no Notion com permissões para ler, criar e atualizar conteúdo. Adicione a conexão à base de cada destino e deixe seu token disponível na variável de ambiente `NOTION_TOKEN`. Na base, copie o ID da fonte em **Manage data sources**. [Conexões internas](https://developers.notion.com/guides/connections-and-tokens/internal-connections) · [ID da fonte de dados](https://developers.notion.com/reference/retrieve-a-data-source#finding-a-data-source-id)

No zsh, adicione `export NOTION_TOKEN="seu_token"` ao `~/.zshrc`, usando o token da sua conexão. Se a atribuição já estiver no arquivo, acrescente `export` no início da linha. Rode `source ~/.zshrc` para carregar a alteração no terminal atual.

A fonte precisa de uma coluna de título e uma do tipo Status. `win-td destination add trabalho` permite escolher os valores de pendente e concluída e, se existirem, as colunas de data para início e conclusão. Para cadastrar sem interação, informe `--data-source-id`, `--pending-state` e `--completed-state`. Consulte `win-td destination add --help` para as demais opções.

## Uso diário

```sh
win-td use trabalho
win-td add "Pedir orçamento ao fornecedor"
win-td list
win-td update
win-td done
```

- `show`, `update`, `done` e `delete` abrem uma busca por título quando você omite o ID em um terminal interativo POSIX. Com ID, atuam diretamente. `delete` envia a tarefa para a lixeira do Notion.
- Use `win-td list --all` para incluir concluídas. Se a tarefa já tiver um horário de início ou conclusão, executar `update --start-now` ou `done` novamente preserva o valor anterior.
- Use `win-td update --help` para ver as opções das colunas de seleção múltipla. Uma coluna `Tag` gera `--tag`. Ao acrescentar um valor novo à coluna, o CLI pede confirmação no terminal.
- Use `win-td destination refresh` depois de alterar as colunas na base. Se precisar refazer o mapeamento de estado ou de horários, use `win-td destination add trabalho --replace`.
- Use `win-td destination remove` em um terminal interativo para buscar um destino pelo nome, selecioná-lo e confirmar a remoção do cadastro local. `win-td destination remove DESTINO` remove diretamente pelo nome. A base e as tarefas ficam no Notion. Se remover o destino ativo, escolha outro com `win-td use DESTINO`.
- Para apontar para outra fonte em uma execução, use `--data-source-id` ou `WIN_TD_DATA_SOURCE_ID`. A prioridade é parâmetro, variável de ambiente e destino ativo. Para uma fonte ainda sem cadastro, o CLI usa o mapeamento do destino ativo e procura as colunas pelos respectivos nomes.

A configuração fica em `~/.config/win-td/config.json` por padrão. Ela respeita `XDG_CONFIG_HOME` e pode ser redirecionada com `WIN_TD_CONFIG`. O token vem do ambiente a cada execução.

## Testes

Rode `python -m unittest discover -v`. Os testes usam uma API do Notion simulada localmente e um terminal de teste para as operações interativas.
