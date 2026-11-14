using System.Diagnostics;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.System;
using Windows.UI;

namespace Kivi.WinUI;

public sealed partial class MainWindow : Window
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMinutes(5) };
    private string _baseUrl = "http://127.0.0.1:8765";
    private Process? _ownedBackend;
    private bool _sending;
    private Border? _pendingBubble;
    private bool _autoSpeak;

    public MainWindow()
    {
        InitializeComponent();
    }

    private async void Root_Loaded(object sender, RoutedEventArgs e)
    {
        var found = await FindApiAsync();
        if (!found)
        {
            ConnectionText.Text = "Запускаю локальный сервер…";
            StartBackend();
            found = await FindApiAsync(40);
        }
        if (!found)
        {
            ConnectionText.Text = "API недоступен";
            return;
        }
        using var health = await GetJsonAsync("/health");
        if (health is not null && health.RootElement.TryGetProperty("version", out var version))
            VersionText.Text = $"онлайн · v{version.GetString()}";
        ConnectionText.Text = $"API: {_baseUrl.Replace("http://", "")}";
        await LoadHistoryAsync();
    }

    private async Task<bool> FindApiAsync(int attempts = 1)
    {
        for (var attempt = 0; attempt < attempts; attempt++)
        {
            foreach (var port in new[] { 8765, 8766, 8767 })
            {
                var candidate = $"http://127.0.0.1:{port}";
                try
                {
                    using var response = await Http.GetAsync($"{candidate}/health");
                    if (!response.IsSuccessStatusCode) continue;
                    _baseUrl = candidate;
                    return true;
                }
                catch
                {
                    // try next
                }
            }
            if (attempt + 1 < attempts) await Task.Delay(500);
        }
        return false;
    }

    private void StartBackend()
    {
        var root = FindProjectRoot();
        if (root is null) return;
        var pythonw = Path.Combine(root, "runtime", "pythonw.exe");
        if (!File.Exists(pythonw)) pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(pythonw)) return;
        var start = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = "-m kivi ui --no-browser --port 8765",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        start.Environment["PYTHONPATH"] = Path.Combine(root, "src") + Path.PathSeparator
            + Path.Combine(root, "runtime", "Lib", "site-packages");
        _ownedBackend = Process.Start(start);
    }

    private static string? FindProjectRoot()
    {
        foreach (var start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            var current = new DirectoryInfo(start);
            while (current is not null)
            {
                if (File.Exists(Path.Combine(current.FullName, "runtime", "pythonw.exe"))
                    || File.Exists(Path.Combine(current.FullName, ".venv", "Scripts", "pythonw.exe")))
                    return current.FullName;
                current = current.Parent;
            }
        }
        return null;
    }

    private async Task<JsonDocument?> GetJsonAsync(string path)
    {
        try
        {
            using var response = await Http.GetAsync(_baseUrl + path);
            var body = await response.Content.ReadAsStringAsync();
            return response.IsSuccessStatusCode ? JsonDocument.Parse(body) : null;
        }
        catch
        {
            return null;
        }
    }

    private async Task<JsonDocument?> PostJsonAsync(string path, object payload)
    {
        try
        {
            using var response = await Http.PostAsJsonAsync(_baseUrl + path, payload);
            var body = await response.Content.ReadAsStringAsync();
            return response.IsSuccessStatusCode ? JsonDocument.Parse(body) : null;
        }
        catch
        {
            return null;
        }
    }

    private async Task LoadHistoryAsync()
    {
        using var doc = await GetJsonAsync("/history?limit=50");
        if (doc is null || !doc.RootElement.TryGetProperty("messages", out var messages)) return;
        MessagesPanel.Children.Clear();
        MessagesPanel.Children.Add(DateLabel("ИСТОРИЯ"));
        foreach (var message in messages.EnumerateArray())
        {
            var role = message.TryGetProperty("role", out var r) ? r.GetString() : "";
            var content = message.TryGetProperty("content", out var c) ? c.GetString() : "";
            if (!string.IsNullOrWhiteSpace(content)) AddBubble(content!, role == "user");
        }
    }

    private void NewChat_Click(object sender, RoutedEventArgs e)
    {
        MessagesPanel.Children.Clear();
        MessagesPanel.Children.Add(DateLabel("НОВЫЙ ЧАТ"));
        AddBubble("Привет! Я Kivi. Чем помочь?", false);
        ComposerInput.Focus(FocusState.Programmatic);
    }

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button button) return;
        var tag = button.Tag?.ToString() ?? "chat";
        var titles = new Dictionary<string, string>
        {
            ["chat"] = "Чат", ["voices"] = "Голоса", ["media"] = "Медиа",
            ["files"] = "Файлы", ["crm"] = "CRM", ["settings"] = "Настройки",
        };
        PageTitle.Text = titles.GetValueOrDefault(tag, "Kivi");
        ChatView.Visibility = tag == "chat" ? Visibility.Visible : Visibility.Collapsed;
        PageView.Visibility = tag == "chat" ? Visibility.Collapsed : Visibility.Visible;
        if (tag != "chat") _ = BuildPageAsync(tag);
    }

    private async Task BuildPageAsync(string tag)
    {
        PagePanel.Children.Clear();
        switch (tag)
        {
            case "voices": await BuildVoicesAsync(); break;
            case "settings": await BuildSettingsAsync(); break;
            case "media": await BuildMediaAsync(); break;
            case "files": await BuildFilesAsync(); break;
            case "crm": await BuildCrmAsync(); break;
        }
    }

    private void BuildPlaceholder(string title, string text)
    {
        PagePanel.Children.Add(Heading(title));
        PagePanel.Children.Add(Sub(text));
    }

    private TextBlock Heading(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
        FontSize = 24,
        FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
    };

    private TextBlock Sub(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
        FontSize = 13,
        TextWrapping = TextWrapping.Wrap,
        Margin = new Thickness(0, 8, 0, 14),
    };

    private Button PageButton(string text, RoutedEventHandler click)
    {
        var button = new Button
        {
            Content = text,
            Background = new SolidColorBrush(Color.FromArgb(255, 11, 58, 52)),
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            BorderThickness = new Thickness(0),
            Padding = new Thickness(12, 8, 12, 8),
            Margin = new Thickness(0, 0, 8, 8),
            HorizontalAlignment = HorizontalAlignment.Left,
        };
        button.Click += click;
        return button;
    }

    private async Task BuildVoicesAsync()
    {
        PagePanel.Children.Add(Heading("Голоса"));
        PagePanel.Children.Add(Sub("Выбери голосовой движок. Активный голос хранится на сервере."));
        using var doc = await GetJsonAsync("/voices");
        if (doc is null) { PagePanel.Children.Add(Sub("Не удалось загрузить голоса.")); return; }
        if (!doc.RootElement.TryGetProperty("voices", out var voices)) return;
        foreach (var voice in voices.EnumerateArray())
        {
            var id = voice.TryGetProperty("id", out var i) ? i.GetString() ?? "" : "";
            var name = voice.TryGetProperty("name", out var n) ? n.GetString() ?? id : id;
            var engine = voice.TryGetProperty("engine", out var e) ? e.GetString() ?? "" : "";
            var active = voice.TryGetProperty("active", out var a) && a.GetBoolean();
            var available = !voice.TryGetProperty("available", out var av) || av.GetBoolean();
            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 12, Padding = new Thickness(12) };
            row.Children.Add(new TextBlock
            {
                Text = $"{name} · {engine}",
                Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
                FontSize = 15,
                VerticalAlignment = VerticalAlignment.Center,
                Width = 360,
            });
            var select = PageButton(active ? "Активен" : available ? "Выбрать" : "Недоступен", async (_, _) =>
            {
                if (!available) return;
                using var result = await PostJsonAsync("/voices/active", new { id });
                await BuildPageAsync("voices");
            });
            select.IsEnabled = available && !active;
            row.Children.Add(select);
            PagePanel.Children.Add(new Border
            {
                Background = new SolidColorBrush(Color.FromArgb(255, 8, 47, 43)),
                BorderBrush = new SolidColorBrush(Color.FromArgb(255, 22, 68, 61)),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(12),
                Margin = new Thickness(0, 0, 0, 10),
                Child = row,
            });
        }
    }

    private async Task BuildSettingsAsync()
    {
        PagePanel.Children.Add(Heading("Настройки"));
        PagePanel.Children.Add(Sub("Провайдер и параметры синтеза речи."));
        using var doc = await GetJsonAsync("/settings");
        if (doc is null) { PagePanel.Children.Add(Sub("Не удалось загрузить настройки.")); return; }
        var providers = new[] { "qwen", "silero", "piper", "fish" };
        var combo = new ComboBox { Width = 260, HorizontalAlignment = HorizontalAlignment.Left, Margin = new Thickness(0, 0, 0, 8) };
        foreach (var provider in providers) combo.Items.Add(provider);
        var current = doc.RootElement.TryGetProperty("voice_provider", out var p) ? p.GetString() : "qwen";
        combo.SelectedItem = providers.Contains(current) ? current : "qwen";
        PagePanel.Children.Add(combo);
        var model = SettingsBox(doc, "model");
        var speaker = SettingsBox(doc, "speaker");
        var rate = SettingsBox(doc, "sample_rate");
        PagePanel.Children.Add(SettingsLabel("model")); PagePanel.Children.Add(model);
        PagePanel.Children.Add(SettingsLabel("speaker")); PagePanel.Children.Add(speaker);
        PagePanel.Children.Add(SettingsLabel("sample_rate")); PagePanel.Children.Add(rate);
        PagePanel.Children.Add(PageButton("Сохранить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/settings", new
            {
                voice_provider = combo.SelectedItem?.ToString(),
                model = model.Text,
                speaker = speaker.Text,
                sample_rate = int.TryParse(rate.Text, out var value) ? value : 48000,
            });
            await BuildPageAsync("settings");
        }));
    }

    private TextBlock SettingsLabel(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
        FontSize = 11,
    };

    private TextBox SettingsBox(JsonDocument doc, string key)
    {
        var value = doc.RootElement.TryGetProperty(key, out var property) ? property.ToString() : "";
        return new TextBox
        {
            Text = value,
            Width = 320,
            HorizontalAlignment = HorizontalAlignment.Left,
            Background = new SolidColorBrush(Color.FromArgb(255, 8, 47, 43)),
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            BorderBrush = new SolidColorBrush(Color.FromArgb(255, 22, 68, 61)),
            Margin = new Thickness(0, 0, 0, 8),
        };
    }

    private async Task BuildMediaAsync()
    {
        PagePanel.Children.Add(Heading("Медиа"));
        PagePanel.Children.Add(Sub("Генерация изображений через локальный API."));
        var prompt = new TextBox
        {
            PlaceholderText = "Опиши изображение",
            Width = 560,
            HorizontalAlignment = HorizontalAlignment.Left,
            AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 8),
        };
        PagePanel.Children.Add(prompt);
        var output = new StackPanel();
        PagePanel.Children.Add(PageButton("Сгенерировать изображение", async (_, _) =>
        {
            output.Children.Clear();
            using var result = await PostJsonAsync("/media/image", new
            {
                prompt = prompt.Text,
                width = 512,
                height = 512,
                model = "flux",
            });
            if (result is null) { output.Children.Add(Sub("Генерация недоступна.")); return; }
            var image = result.RootElement.TryGetProperty("image", out var value) ? value.GetString() : null;
            if (image is null || !image.StartsWith("data:image/", StringComparison.Ordinal)) return;
            var comma = image.IndexOf(',');
            if (comma <= 0) return;
            var bytes = Convert.FromBase64String(image[(comma + 1)..]);
            var bitmap = new Microsoft.UI.Xaml.Media.Imaging.BitmapImage();
            using var stream = new Windows.Storage.Streams.InMemoryRandomAccessStream();
            using var writer = new Windows.Storage.Streams.DataWriter(stream);
            writer.WriteBytes(bytes);
            await writer.StoreAsync();
            await writer.FlushAsync();
            writer.DetachStream();
            stream.Seek(0);
            await bitmap.SetSourceAsync(stream);
            output.Children.Add(new Image { Source = bitmap, MaxWidth = 420, MaxHeight = 420, HorizontalAlignment = HorizontalAlignment.Left });
        }));
        PagePanel.Children.Add(output);
        await Task.CompletedTask;
    }

    private async Task BuildFilesAsync()
    {
        PagePanel.Children.Add(Heading("Файлы"));
        PagePanel.Children.Add(Sub("WinUI-файловый менеджер: список рабочей папки."));
        using var doc = await GetJsonAsync("/fs/list?path=.");
        if (doc is null) { PagePanel.Children.Add(Sub("Не удалось получить список.")); return; }
        if (!doc.RootElement.TryGetProperty("entries", out var entries)) return;
        foreach (var entry in entries.EnumerateArray())
        {
            var name = entry.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
            var type = entry.TryGetProperty("type", out var t) ? t.GetString() ?? "file" : "file";
            PagePanel.Children.Add(new TextBlock
            {
                Text = $"{(type == "dir" ? "▸" : "·")} {name}",
                Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
                FontSize = 14,
                Padding = new Thickness(8, 5, 8, 5),
            });
        }
    }

    private async Task BuildCrmAsync()
    {
        PagePanel.Children.Add(Heading("CRM"));
        PagePanel.Children.Add(Sub("Статус AltCRM и карточка заказа."));
        using var doc = await GetJsonAsync("/crm/status");
        var connected = doc?.RootElement.TryGetProperty("connected", out var c) == true && c.GetBoolean();
        PagePanel.Children.Add(Sub(connected ? "Расширение подключено." : "Расширение не подключено."));
        var readButton = PageButton("Прочитать заказ", async (_, _) =>
        {
            using var result = await GetJsonAsync("/crm/read");
            if (result is not null) PagePanel.Children.Add(Sub(result.RootElement.ToString()));
        });
        PagePanel.Children.Add(readButton);
        var comment = new TextBox { PlaceholderText = "Комментарий", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        var deadline = new TextBox { PlaceholderText = "Согласованные сроки ремонта", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        PagePanel.Children.Add(comment);
        PagePanel.Children.Add(deadline);
        PagePanel.Children.Add(PageButton("Заполнить", async (_, _) =>
        {
            using var result = await PostJsonAsync("/crm/fill", new
            {
                comment = comment.Text,
                checklist = new Dictionary<string, string> { ["deadline"] = deadline.Text },
            });
            PagePanel.Children.Add(Sub(result is null ? "Не удалось заполнить." : "Заполнено, не сохранено."));
        }));
        var task = new TextBox { PlaceholderText = "Текст задачи", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        var when = new TextBox { PlaceholderText = "Срок, например завтра", Width = 420, HorizontalAlignment = HorizontalAlignment.Left };
        PagePanel.Children.Add(task);
        PagePanel.Children.Add(when);
        PagePanel.Children.Add(PageButton("Заполнить задачу", async (_, _) =>
        {
            using var result = await PostJsonAsync("/crm/task", new { comment = task.Text, when_text = when.Text });
            PagePanel.Children.Add(Sub(result is null ? "Не удалось заполнить." : "Задача заполнена, не создана."));
        }));
    }

    private static TextBlock DateLabel(string text) => new()
    {
        Text = text,
        Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
        FontSize = 11,
        HorizontalAlignment = HorizontalAlignment.Center,
        Margin = new Thickness(0, 4, 0, 22),
    };

    private void ComposerInput_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != VirtualKey.Enter) return;
        var shift = Microsoft.UI.Input.InputKeyboardSource
            .GetKeyStateForCurrentThread(VirtualKey.Shift)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        if (shift) return;
        e.Handled = true;
        _ = SendMessageAsync();
    }

    private void Send_Click(object sender, RoutedEventArgs e) => _ = SendMessageAsync();
    private void Mic_Click(object sender, RoutedEventArgs e) { }
    private void Wake_Click(object sender, RoutedEventArgs e) { }
    private void Speak_Click(object sender, RoutedEventArgs e) => _autoSpeak = !_autoSpeak;

    private async Task SendMessageAsync()
    {
        if (_sending) return;
        var text = ComposerInput.Text.Trim();
        if (text.Length == 0) return;
        _sending = true;
        ComposerInput.Text = "";
        AddBubble(text, true);
        AddBubble("…", false, true);
        try
        {
            using var response = await Http.PostAsJsonAsync($"{_baseUrl}/chat", new { message = text });
            var body = await response.Content.ReadAsStringAsync();
            if (!response.IsSuccessStatusCode)
            {
                ReplacePending($"Ошибка сервера: HTTP {(int)response.StatusCode}");
                return;
            }
            using var doc = JsonDocument.Parse(body);
            var answer = doc.RootElement.TryGetProperty("answer", out var value) ? value.GetString() : null;
            ReplacePending(string.IsNullOrWhiteSpace(answer) ? "Пустой ответ." : answer!);
        }
        catch (Exception ex)
        {
            ReplacePending($"Не удалось связаться с Kivi: {ex.Message}");
        }
        finally
        {
            _sending = false;
        }
    }

    private void AddBubble(string text, bool fromUser, bool pending = false)
    {
        var content = new StackPanel();
        content.Children.Add(new TextBlock
        {
            Text = text,
            Foreground = new SolidColorBrush(Color.FromArgb(255, 228, 247, 241)),
            FontSize = 14,
            TextWrapping = TextWrapping.Wrap,
        });
        content.Children.Add(new TextBlock
        {
            Text = $"{(fromUser ? "Вы" : "Kivi")} · {DateTime.Now:HH:mm}",
            Foreground = new SolidColorBrush(Color.FromArgb(255, 143, 184, 176)),
            FontSize = 10,
            Margin = new Thickness(0, 6, 0, 0),
        });
        var bubble = new Border
        {
            Background = new SolidColorBrush(fromUser
                ? Color.FromArgb(255, 10, 58, 52)
                : Color.FromArgb(255, 8, 47, 43)),
            BorderBrush = new SolidColorBrush(Color.FromArgb(255, 22, 68, 61)),
            BorderThickness = new Thickness(fromUser ? 0 : 1),
            CornerRadius = new CornerRadius(fromUser ? 15 : 4, 15, 15, 15),
            Padding = new Thickness(16, 12, 16, 12),
            MaxWidth = 620,
            HorizontalAlignment = fromUser ? HorizontalAlignment.Right : HorizontalAlignment.Left,
            Child = content,
        };
        MessagesPanel.Children.Add(bubble);
        if (pending) _pendingBubble = bubble;
        MessagesScroll.ChangeView(null, MessagesScroll.ScrollableHeight, null);
    }

    private void ReplacePending(string text)
    {
        if (_pendingBubble?.Child is StackPanel content && content.Children[0] is TextBlock block)
        {
            block.Text = text;
            _pendingBubble = null;
        }
        MessagesScroll.ChangeView(null, MessagesScroll.ScrollableHeight, null);
    }
}
