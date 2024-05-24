import os
import re
import plotly.graph_objects as go
import chart_studio

chart_studio.tools.set_credentials_file(username='tbsfchnr', api_key='YGDrRHkapBeeGqY3nZcp')

# Get a list of all Markdown files in the current directory
md_files = [file for file in os.listdir() if file.endswith('.md')]

# Initialize lists to store data from all files
all_names = [md_file.split('.')[0] for md_file in md_files]
all_days = []
all_hours = []

# Loop through each Markdown file
for md_file in md_files:
    with open(md_file, 'r') as f:
        md_content = f.read()

    # Extract data from Markdown content using regular expressions
    matches = re.findall(r'\|\s*([\w\s]+?)\s*\|\s*(\d+)\s*\|', md_content)

    # Extracting day and hours data from matches
    all_days.append([match[0] for match in matches])
    all_hours.append([int(match[1]) for match in matches])

# Calculate the sums of each list of hours
all_hours_sums = [sum(hours) for hours in all_hours]

# Sort all_hours, all_days, and all_names based on the sums of hours in descending order
all_hours_sorted, all_days_sorted, all_names_sorted = zip(*sorted(zip(all_hours, all_days, all_names), key=lambda x: sum(x[0]), reverse=True))



# Create multi-trace polar chart
fig = go.Figure()

for hours, days, name in zip(all_hours_sorted, all_days_sorted, all_names_sorted):
    print(hours, days, name)
    fig.add_trace(go.Scatterpolar(
        r=hours,
        theta=days,
        fill='toself',
        name=name
    ))

fig.update_layout(
    polar=dict(
        radialaxis=dict(
            visible=True,
        ),
    ),
    showlegend=True,
)

fig.update_polars(angularaxis_direction="clockwise")

fig.update_traces(hovertemplate="Hours: %{r} <extra>Task IDs: 7, 9, 13\nMentions: @regenshik, @fortyfoxes</extra>", line_shape="spline")

# Write HTML export to Plotly Chart Studio
url = chart_studio.plotly.plot(fig, filename="crew", auto_open=False)

# Embed URL in an iframe and replace it in the Markdown content
iframe_tag = f'<iframe src="{url}" width="100%" height="400px"></iframe>'

data = all_hours_sorted, all_days_sorted, all_names_sorted

with open("Crew.md", 'w') as f:
    f.write("This page shows an overview of the [[Crew]] energy contributions:\n\n ")
    f.write("| Person | Mon | Tue | Wed | Thu | Fri | Sat | Sun |\n")
    f.write("| :--: | :--: | :--: | :--: | :--: | :--: | :--: | :--: |\n")

    for i, person in enumerate(data[2]):
        f.write(f"| {person} ")  # Write person name
        for j, day in enumerate(data[1][i]):
            f.write(f"| {data[0][i][j]} ")  # Write data for each day
        f.write("|\n")

    f.write("\nThese are visualised on the following interactive energy chart:\n\n")

    f.write(iframe_tag + '\n')
