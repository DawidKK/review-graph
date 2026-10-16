-- Loaded by nvim-review only. Diffview still opens through the existing shell command.
local M = {}
local command = __REVIEW_COMMAND__

local function graph(action)
  local root = vim.fn.getcwd()
  local arg = vim.fn.argv(0)
  if type(arg) == "string" and vim.fn.isdirectory(arg) == 1 then
    root = vim.fn.fnamemodify(arg, ":p")
  end
  vim.fn.jobstart({ command, action, root }, {
    detach = true,
    on_stderr = function(_, lines)
      local message = table.concat(lines, "\n"):gsub("%s+$", "")
      if message ~= "" then
        vim.schedule(function() vim.notify(message, vim.log.levels.WARN) end)
      end
    end,
  })
end

function M.refresh()
  -- Refresh the current view without changing revisions, filters or untracked settings.
  local ok, err = pcall(vim.cmd, "DiffviewRefresh")
  if not ok then vim.notify(tostring(err), vim.log.levels.WARN) end
  graph("refresh")
end

vim.keymap.set("n", "<leader>gr", M.refresh, { desc = "Odśwież Diffview i graf Graphify" })
vim.keymap.set("n", "<leader>go", function() graph("open") end, { desc = "Otwórz graf Graphify" })

if vim.env.REVIEW_GRAPH_START == "1" then
  vim.api.nvim_create_autocmd("VimEnter", {
    once = true,
    callback = function() vim.schedule(function() graph("start") end) end,
  })
end

return M
