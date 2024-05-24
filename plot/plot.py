import os
import plotly.graph_objects as go
import pandas as pd

# Get a list of all CSV files in the current directory
csv_files = [file for file in os.listdir() if file.endswith('.csv')]

print(f"Found {len(csv_files)} csv files.")

# Loop through each CSV file
for csv_file in csv_files:
    # Read data from CSV
    data = pd.read_csv(csv_file)

    print(f"{data.shape[0]} rows for {csv_file}")

    # Create pie chart
    fig = go.Figure(data=[go.Pie(labels=data['Day'], values=data['Hours'])])

    # Customize layout
    fig.update_layout(title='Weekly Work Hours',
                      font=dict(family='Arial, sans-serif', size=12),
                      margin=dict(t=50, b=10, l=10, r=10))

    # Write HTML export
    html_file = os.path.splitext(csv_file)[0] + '.html'
    print(f"Printing HTML to {html_file}")
    fig.write_html(html_file)
